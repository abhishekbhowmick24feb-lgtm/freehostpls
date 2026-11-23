"""Marketplace panel: a heading, category sections and priced items."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import Optional

import discord
from discord.ext import commands

from core.database import DEFAULT_SHOP
from core.media import (
    banner_change,
    banner_file,
    banner_from_input,
    banner_url,
)
from core.utils import (
    ConfirmView,
    error_embed,
    info_embed,
    parse_color,
    success_embed,
    truncate,
)

log = logging.getLogger(__name__)

MAX_EMBEDS = 10  # Discord's hard limit per message
MAX_SECTIONS = 8  # heading + optional banner leaves room for the sections


# ---------------------------------------------------------------- rendering


def build_shop_embeds(
    shop: dict,
) -> tuple[list[discord.Embed], list[discord.File]]:
    heading = shop.get("heading") or {}
    color = parse_color(heading.get("color"))

    embeds: list[discord.Embed] = []
    files: list[discord.File] = []

    # A banner-only embed puts the GIF/picture at the very top of the panel.
    banner_link = banner_url(heading.get("image"))
    if banner_link:
        banner = discord.Embed(color=color)
        banner.set_image(url=banner_link)
        embeds.append(banner)
        banner_upload = banner_file(heading.get("image"))
        if banner_upload is not None and banner_upload.filename not in {
            file.filename for file in files
        }:
            files.append(banner_upload)

    main = discord.Embed(color=color)
    main.title = truncate(heading.get("title") or "🛒 Marketplace", 256)
    if heading.get("description"):
        main.description = truncate(heading["description"], 4000)
    if heading.get("thumbnail"):
        main.set_thumbnail(url=heading["thumbnail"])
    if heading.get("footer"):
        main.set_footer(text=truncate(heading["footer"], 2048))
    embeds.append(main)

    for section in shop.get("sections", []):
        name = f"{section.get('emoji', '').strip()} {section.get('name', 'Section')}".strip()
        embed = discord.Embed(
            title=truncate(name, 256),
            color=parse_color(section.get("color"), color),
        )
        parts: list[str] = []
        if section.get("description"):
            parts.append(truncate(section["description"], 1000))

        items = section.get("items") or []
        if items:
            lines = []
            for item in items:
                emoji = f"{item.get('emoji', '').strip()} " if item.get("emoji") else ""
                line = (
                    f"{emoji}**{truncate(item.get('name', 'Item'), 100)}** — "
                    f"{truncate(item.get('price', '—'), 64)}"
                )
                if item.get("stock"):
                    line += f"  `{truncate(item['stock'], 64)}`"
                if item.get("description"):
                    line += f"\n> {truncate(item['description'], 250)}"
                lines.append(line)
            parts.append("\n\n".join(lines))
        else:
            parts.append("*No items listed yet.*")

        embed.description = truncate("\n\n".join(parts), 4000)
        section_link = banner_url(section.get("image"))
        if section_link:
            embed.set_image(url=section_link)
            section_upload = banner_file(section.get("image"))
            if section_upload is not None and section_upload.filename not in {
                file.filename for file in files
            }:
                files.append(section_upload)
        embeds.append(embed)

    return embeds[:MAX_EMBEDS], files


def split_spec(spec: str) -> list[str]:
    return [part.strip() for part in spec.split("|")]


class ShopStaffOnly(commands.CheckFailure):
    """Raised when a member may not manage the marketplace."""


def is_shop_staff():
    """Allow the configured shop role, administrators and the guild owner.

    When no shop role is configured the command falls back to Manage Server so
    the feature works out of the box before an admin sets a role.
    """

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False
        member = ctx.author
        if member.id == ctx.guild.owner_id or member.guild_permissions.administrator:
            return True

        role_id = ctx.bot.db.guild(ctx.guild.id)["shop"].get("staff_role_id")
        if role_id:
            role = ctx.guild.get_role(role_id)
            if role is not None and role in member.roles:
                return True
            hint = role.mention if role is not None else "the shop staff role"
            raise ShopStaffOnly(
                f"Only {hint} can manage the marketplace.\n"
                f"Ask an administrator to run `{ctx.prefix}shop role @Role`."
            )

        if member.guild_permissions.manage_guild:
            return True
        raise ShopStaffOnly(
            "Only shop staff can manage the marketplace.\n"
            f"Ask an administrator to run `{ctx.prefix}shop role @Role`."
        )

    return commands.check(predicate)


def is_hex(value: str) -> bool:
    cleaned = value.strip().lstrip("#")
    if len(cleaned) != 6:
        return False
    try:
        int(cleaned, 16)
    except ValueError:
        return False
    return True


class Marketplace(commands.Cog):
    """Build a professional marketplace panel with sections, items and prices."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # --------------------------------------------------------------- helpers

    def _shop(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)["shop"]

    @staticmethod
    def _resolve_section(shop: dict, query: str) -> dict:
        sections = shop["sections"]
        cleaned = query.strip().lower()
        if cleaned.isdigit() and 1 <= int(cleaned) <= len(sections):
            return sections[int(cleaned) - 1]
        for section in sections:
            if section["name"].lower() == cleaned:
                return section
        for section in sections:
            if cleaned in section["name"].lower():
                return section
        raise commands.BadArgument(f"No section matching **{query}** found.")

    @staticmethod
    def _resolve_item(section: dict, query: str) -> dict:
        items = section["items"]
        cleaned = query.strip().lower()
        if cleaned.isdigit() and 1 <= int(cleaned) <= len(items):
            return items[int(cleaned) - 1]
        for item in items:
            if item["name"].lower() == cleaned:
                return item
        for item in items:
            if cleaned in item["name"].lower():
                return item
        raise commands.BadArgument(
            f"No item matching **{query}** in *{section['name']}*."
        )

    async def sync_panel(self, guild: discord.Guild) -> bool:
        """Edit the posted panel in place. Returns False when there is none."""
        shop = self._shop(guild)
        channel = guild.get_channel(shop.get("channel_id") or 0)
        if not isinstance(channel, discord.TextChannel) or not shop.get("message_id"):
            return False
        try:
            message = await channel.fetch_message(shop["message_id"])
            embeds, files = build_shop_embeds(shop)
            await message.edit(embeds=embeds, attachments=files)
            return True
        except discord.HTTPException as exc:
            log.warning("Could not update marketplace panel: %s", exc)
            return False

    async def _finish(self, ctx: commands.Context, text: str) -> None:
        await self.bot.db.save()
        if await self.sync_panel(ctx.guild):
            suffix = " The panel was updated."
        else:
            suffix = f" Run `{ctx.prefix}shop post` to publish the panel."
        await self.bot.audit(
            ctx.guild,
            title="🛒 Marketplace updated",
            description=text,
            color=0x2ECC71,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(embed=success_embed(text + suffix), mention_author=False)

    async def _ask(
        self, ctx: commands.Context, prompt: str, *, timeout: float = 120.0
    ) -> str | None:
        """Ask the author one question and return their next message."""
        await ctx.reply(embed=info_embed(prompt), mention_author=False)

        def check(message: discord.Message) -> bool:
            return (
                not message.author.bot
                and message.author.id == ctx.author.id
                and message.channel.id == ctx.channel.id
            )

        try:
            message = await self.bot.wait_for("message", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            await ctx.send(
                embed=error_embed(
                    f"Timed out after {int(timeout)}s. "
                    f"Run `{ctx.prefix}shop additem` to start again."
                )
            )
            return None

        answer = message.content.strip()
        if answer.lower() in {"cancel", "stop", "quit"}:
            await message.reply(embed=error_embed("Cancelled — nothing was added."))
            return None
        return answer

    async def _additem_wizard(self, ctx: commands.Context) -> None:
        """Walk the member through adding an item, one question at a time."""
        shop = self._shop(ctx.guild)
        sections = shop["sections"]
        if not sections:
            await ctx.reply(
                embed=error_embed(
                    "No sections yet — create one first, then add items to it.\n"
                    f"Example: `{ctx.prefix}shop addsection Nitro | 🚀`\n"
                    f"Then run `{ctx.prefix}shop additem` again."
                ),
                mention_author=False,
            )
            return

        if len(sections) == 1:
            section = sections[0]
            await ctx.reply(
                embed=info_embed(
                    f"Adding a new item to **{section['name']}**.\n"
                    "Answer each question below, or type `cancel` to stop.",
                    title="🛒 Guided item setup",
                ),
                mention_author=False,
            )
        else:
            listing = "\n".join(
                f"`{index}.` {entry['name']}"
                for index, entry in enumerate(sections, start=1)
            )
            section = None
            for _ in range(3):
                answer = await self._ask(
                    ctx,
                    "**Section**\nWhich section should hold this item?\n"
                    f"{listing}\n\nReply with a number or name, or type `cancel`.",
                )
                if answer is None:
                    return
                try:
                    section = self._resolve_section(shop, answer)
                    break
                except commands.BadArgument as exc:
                    await ctx.reply(embed=error_embed(str(exc)), mention_author=False)
            if section is None:
                await ctx.reply(
                    embed=error_embed("Cancelled — nothing was added."),
                    mention_author=False,
                )
                return

        name = ""
        while not name:
            answer = await self._ask(
                ctx, "**Item name**\nWhat is this item called?"
            )
            if answer is None:
                return
            name = truncate(answer, 100)

        price = ""
        while not price:
            answer = await self._ask(
                ctx, f"**Price**\nHow much does **{name}** cost? e.g. `$4.99`"
            )
            if answer is None:
                return
            price = truncate(answer, 64)

        answer = await self._ask(
            ctx,
            "**Stock** *(optional)*\n"
            "Type a status like `In stock` or `2 left`, or `skip`.",
        )
        if answer is None:
            return
        stock = "" if answer.lower() in {"skip", "none", "-"} else truncate(answer, 64)

        answer = await self._ask(
            ctx,
            "**Description** *(optional)*\nType a short note, or `skip`.",
        )
        if answer is None:
            return
        description = (
            "" if answer.lower() in {"skip", "none", "-"} else truncate(answer, 250)
        )

        section["items"].append(
            {
                "name": name,
                "price": price,
                "stock": stock,
                "description": description,
                "emoji": "",
            }
        )
        await self._finish(ctx, f"Added **{name}** ({price}) to **{section['name']}**.")

    async def _delete_panel(self, guild: discord.Guild) -> None:
        shop = self._shop(guild)
        channel = guild.get_channel(shop.get("channel_id") or 0)
        if isinstance(channel, discord.TextChannel) and shop.get("message_id"):
            try:
                message = await channel.fetch_message(shop["message_id"])
                await message.delete()
            except discord.HTTPException:
                pass
        shop["message_id"] = None

    # ---------------------------------------------------------------- group

    @commands.hybrid_group(
        name="shop",
        aliases=["market", "marketplace", "store"],
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def shop(self, ctx: commands.Context) -> None:
        """Build the marketplace panel for this server."""
        shop = self._shop(ctx.guild)
        channel = ctx.guild.get_channel(shop.get("channel_id") or 0)
        sections = shop.get("sections", [])
        total_items = sum(len(section["items"]) for section in sections)
        staff_role = ctx.guild.get_role(shop.get("staff_role_id") or 0)
        embed = info_embed(
            f"**Panel channel:** {channel.mention if channel else 'not set'}\n"
            f"**Staff role:** {staff_role.mention if staff_role else 'Manage Server (default)'}\n"
            f"**Sections:** `{len(sections)}` • **Items:** `{total_items}`\n"
            f"**Panel:** {'posted' if shop.get('message_id') else 'not posted'}\n\n"
            f"**Staff:** `{ctx.prefix}shop role @Role`\n"
            f"**Setup:** `{ctx.prefix}shop channel #marketplace` then "
            f"`{ctx.prefix}shop post`\n"
            f"**Sections:** `{ctx.prefix}shop addsection name | emoji | description | banner | #color`\n"
            f"**Items:** `{ctx.prefix}shop additem` walks you through it, or use "
            f"`{ctx.prefix}shop additem section | name | price` for a quick add.\n"
            f"Use `{ctx.prefix}help shop` for the full command list.",
            title="🛒 Marketplace",
        )
        await ctx.reply(embed=embed, mention_author=False)

    # --------------------------------------------------------------- heading

    @shop.command(name="channel", usage="<#channel>")
    @commands.guild_only()
    @is_shop_staff()
    async def channel_cmd(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Choose the channel where the panel is posted."""
        shop = self._shop(ctx.guild)
        shop["channel_id"] = channel.id
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(
                f"Marketplace channel set to {channel.mention}. "
                f"Run `{ctx.prefix}shop post` to publish the panel."
            ),
            mention_author=False,
        )

    @shop.command(name="role", usage="<@role|none>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def role_cmd(self, ctx: commands.Context, *, value: str) -> None:
        """Choose the staff role that can manage the marketplace."""
        shop = self._shop(ctx.guild)
        if value.lower() in {"none", "clear", "reset"}:
            shop["staff_role_id"] = None
            await self.bot.db.save()
            await ctx.reply(
                embed=success_embed(
                    "Shop staff role cleared.\n"
                    "Members with **Manage Server** can manage the marketplace again."
                ),
                mention_author=False,
            )
            return
        try:
            role = await commands.RoleConverter().convert(ctx, value)
        except commands.BadArgument as exc:
            raise commands.BadArgument(
                "Mention a role, e.g. `shop role @Shop Staff`."
            ) from exc
        if role.is_default() or role.managed:
            raise commands.BadArgument("That role cannot manage the marketplace.")
        shop["staff_role_id"] = role.id
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(
                f"**{role.mention}** can now manage the marketplace.\n"
                "Administrators and the server owner always keep access.",
                title="Shop staff role set",
            ),
            mention_author=False,
        )

    @shop.command(name="heading", usage="<title>")
    @commands.guild_only()
    @is_shop_staff()
    async def heading_cmd(self, ctx: commands.Context, *, title: str) -> None:
        """Set the big title at the top of the panel."""
        self._shop(ctx.guild)["heading"]["title"] = truncate(title, 256)
        await self._finish(ctx, f"Heading set to **{truncate(title, 100)}**.")

    @shop.command(name="description", usage="<text>")
    @commands.guild_only()
    @is_shop_staff()
    async def description_cmd(self, ctx: commands.Context, *, text: str) -> None:
        """Set the text under the heading. Use \\n for a new line."""
        heading = self._shop(ctx.guild)["heading"]
        if text.lower() in {"none", "clear", "reset"}:
            heading["description"] = ""
        else:
            heading["description"] = truncate(text.replace("\\n", "\n"), 4000)
        await self._finish(ctx, "Marketplace description updated.")

    @shop.command(name="banner", usage="<url|none>")
    @commands.guild_only()
    @is_shop_staff()
    async def banner_cmd(
        self,
        ctx: commands.Context,
        *,
        url: str | None = None,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Put a GIF or picture at the very top of the panel."""
        heading = self._shop(ctx.guild)["heading"]
        value, error = await banner_from_input(ctx, attachment, url)
        if error is not None:
            raise commands.BadArgument(error)
        heading["image"] = value
        await self._finish(ctx, f"Panel banner {banner_change(value)}.")

    @shop.command(name="thumbnail", usage="<url|none>")
    @commands.guild_only()
    @is_shop_staff()
    async def thumbnail_cmd(self, ctx: commands.Context, *, url: str) -> None:
        """Set the small logo in the corner of the heading."""
        heading = self._shop(ctx.guild)["heading"]
        if url.lower() in {"none", "clear", "reset"}:
            heading["thumbnail"] = ""
            await self._finish(ctx, "Thumbnail removed.")
            return
        if not url.startswith(("http://", "https://")):
            raise commands.BadArgument("The thumbnail must be a direct http(s) image URL.")
        heading["thumbnail"] = url.strip()
        await self._finish(ctx, "Thumbnail updated.")

    @shop.command(name="footer", usage="<text>")
    @commands.guild_only()
    @is_shop_staff()
    async def footer_cmd(self, ctx: commands.Context, *, text: str) -> None:
        """Set the small footer line of the heading."""
        heading = self._shop(ctx.guild)["heading"]
        if text.lower() in {"none", "clear", "reset"}:
            heading["footer"] = ""
        else:
            heading["footer"] = truncate(text, 2048)
        await self._finish(ctx, "Footer updated.")

    @shop.command(name="color", usage="<#hex>")
    @commands.guild_only()
    @is_shop_staff()
    async def color_cmd(self, ctx: commands.Context, hex_code: str) -> None:
        """Set the default panel colour, e.g. #5865F2."""
        if not is_hex(hex_code):
            raise commands.BadArgument("Use a 6 character hex colour, like `#5865F2`.")
        self._shop(ctx.guild)["heading"]["color"] = f"#{hex_code.strip().lstrip('#')}"
        await self._finish(ctx, f"Panel colour set to `#{hex_code.strip().lstrip('#')}`.")

    # -------------------------------------------------------------- sections

    @shop.command(name="addsection", usage="<name> | [emoji] | [description] | [banner] | [#color]")
    @commands.guild_only()
    @is_shop_staff()
    async def addsection(
        self,
        ctx: commands.Context,
        *,
        spec: str,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Add a category section: what type of items it holds, its GIF and colour."""
        parts = split_spec(spec)
        if not parts or not parts[0]:
            raise commands.BadArgument(
                "Use `addsection name | emoji | description | banner url | #colour`."
            )
        shop = self._shop(ctx.guild)
        if len(shop["sections"]) >= MAX_SECTIONS:
            raise commands.BadArgument(
                f"Discord allows {MAX_EMBEDS} embeds per message, so the panel fits "
                f"**{MAX_SECTIONS}** sections. Remove one first."
            )

        name = truncate(parts[0], 100)
        emoji = parts[1] if len(parts) > 1 else ""
        description = parts[2] if len(parts) > 2 else ""
        banner = parts[3] if len(parts) > 3 else ""
        color = parts[4] if len(parts) > 4 else ""
        banner, error = await banner_from_input(
            ctx, attachment, banner, required=False
        )
        if error is not None:
            raise commands.BadArgument(error)
        if color and not is_hex(color):
            raise commands.BadArgument("The section colour must be a hex value like `#FF6B6B`.")

        shop["sections"].append(
            {
                "name": name,
                "emoji": truncate(emoji, 8),
                "description": truncate(description.replace("\\n", "\n"), 1000),
                "image": banner,
                "color": f"#{color.strip().lstrip('#')}" if color else "",
                "items": [],
            }
        )
        await self._finish(ctx, f"Section **{name}** added.")

    @shop.command(name="removesection", usage="<section>")
    @commands.guild_only()
    @is_shop_staff()
    async def removesection(self, ctx: commands.Context, *, section: str) -> None:
        """Delete a section and everything inside it."""
        shop = self._shop(ctx.guild)
        target = self._resolve_section(shop, section)
        if not await ConfirmView.prompt(
            ctx, f"Delete section **{target['name']}** and its items?"
        ):
            return
        shop["sections"].remove(target)
        await self._finish(ctx, f"Section **{target['name']}** removed.")

    @shop.command(name="renamesection", usage="<section> | <new name>")
    @commands.guild_only()
    @is_shop_staff()
    async def renamesection(self, ctx: commands.Context, *, spec: str) -> None:
        """Rename a section."""
        parts = split_spec(spec)
        if len(parts) < 2 or not parts[1]:
            raise commands.BadArgument("Use `renamesection section | new name`.")
        target = self._resolve_section(self._shop(ctx.guild), parts[0])
        target["name"] = truncate(parts[1], 100)
        await self._finish(ctx, f"Section renamed to **{target['name']}**.")

    @shop.command(name="sectiondesc", usage="<section> | <text>")
    @commands.guild_only()
    @is_shop_staff()
    async def sectiondesc(self, ctx: commands.Context, *, spec: str) -> None:
        """Describe what kind of items a section holds."""
        parts = split_spec(spec)
        if len(parts) < 2:
            raise commands.BadArgument("Use `sectiondesc section | text`.")
        target = self._resolve_section(self._shop(ctx.guild), parts[0])
        text = parts[1].strip()
        target["description"] = "" if text.lower() in {"none", "clear", "reset"} else truncate(
            text.replace("\\n", "\n"), 1000
        )
        await self._finish(ctx, f"Description of **{target['name']}** updated.")

    @shop.command(name="sectionbanner", usage="<section> | <url|none>")
    @commands.guild_only()
    @is_shop_staff()
    async def sectionbanner(
        self,
        ctx: commands.Context,
        *,
        spec: str | None = None,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Set the GIF/picture shown above a section's items."""
        parts = split_spec(spec or "")
        if not parts or not parts[0]:
            raise commands.BadArgument(
                "Use `sectionbanner section | image url`, or attach a GIF."
            )
        target = self._resolve_section(self._shop(ctx.guild), parts[0])
        value, error = await banner_from_input(
            ctx, attachment, parts[1] if len(parts) > 1 else None
        )
        if error is not None:
            raise commands.BadArgument(error)
        target["image"] = value
        await self._finish(
            ctx, f"Banner of **{target['name']}** {banner_change(value)}."
        )

    @shop.command(name="sectioncolor", usage="<section> | <#hex>")
    @commands.guild_only()
    @is_shop_staff()
    async def sectioncolor(self, ctx: commands.Context, *, spec: str) -> None:
        """Set a section's embed colour."""
        parts = split_spec(spec)
        if len(parts) < 2 or not is_hex(parts[1]):
            raise commands.BadArgument("Use `sectioncolor section | #FF6B6B`.")
        target = self._resolve_section(self._shop(ctx.guild), parts[0])
        target["color"] = f"#{parts[1].strip().lstrip('#')}"
        await self._finish(ctx, f"Colour of **{target['name']}** updated.")

    # ----------------------------------------------------------------- items

    @shop.command(name="additem", usage="[section | name | price | stock | description]")
    @commands.guild_only()
    @is_shop_staff()
    async def additem(self, ctx: commands.Context, *, spec: str | None = None) -> None:
        """Add a product. Leave it empty for a step-by-step walkthrough."""
        if not spec or not spec.strip():
            await self._additem_wizard(ctx)
            return

        parts = split_spec(spec)
        if len(parts) < 3 or not parts[1] or not parts[2]:
            raise commands.BadArgument(
                "Use `additem section | name | price | stock | description`.\n"
                f"Or just run `{ctx.prefix}shop additem` and I will ask you for "
                "each field one at a time."
            )
        shop = self._shop(ctx.guild)
        section = self._resolve_section(shop, parts[0])
        section["items"].append(
            {
                "name": truncate(parts[1], 100),
                "price": truncate(parts[2], 64),
                "stock": truncate(parts[3], 64) if len(parts) > 3 else "",
                "description": truncate(parts[4], 250) if len(parts) > 4 else "",
                "emoji": "",
            }
        )
        await self._finish(
            ctx, f"Added **{truncate(parts[1], 100)}** ({truncate(parts[2], 64)}) to **{section['name']}**."
        )

    @shop.command(name="removeitem", usage="<section> | <item>")
    @commands.guild_only()
    @is_shop_staff()
    async def removeitem(self, ctx: commands.Context, *, spec: str) -> None:
        """Remove an item from a section."""
        parts = split_spec(spec)
        if len(parts) < 2:
            raise commands.BadArgument("Use `removeitem section | item`.")
        section = self._resolve_section(self._shop(ctx.guild), parts[0])
        item = self._resolve_item(section, parts[1])
        section["items"].remove(item)
        await self._finish(ctx, f"Removed **{item['name']}** from **{section['name']}**.")

    @shop.command(name="setprice", usage="<section> | <item> | <price>")
    @commands.guild_only()
    @is_shop_staff()
    async def setprice(self, ctx: commands.Context, *, spec: str) -> None:
        """Change an item's price."""
        parts = split_spec(spec)
        if len(parts) < 3 or not parts[2]:
            raise commands.BadArgument("Use `setprice section | item | price`.")
        section = self._resolve_section(self._shop(ctx.guild), parts[0])
        item = self._resolve_item(section, parts[1])
        item["price"] = truncate(parts[2], 64)
        await self._finish(ctx, f"**{item['name']}** now costs **{item['price']}**.")

    @shop.command(name="setstock", usage="<section> | <item> | <status>")
    @commands.guild_only()
    @is_shop_staff()
    async def setstock(self, ctx: commands.Context, *, spec: str) -> None:
        """Set an item's stock badge, e.g. `In stock` or `Out of stock`."""
        parts = split_spec(spec)
        if len(parts) < 3:
            raise commands.BadArgument("Use `setstock section | item | status`.")
        section = self._resolve_section(self._shop(ctx.guild), parts[0])
        item = self._resolve_item(section, parts[1])
        status = parts[2].strip()
        item["stock"] = "" if status.lower() in {"none", "clear", "reset"} else truncate(status, 64)
        await self._finish(ctx, f"Stock of **{item['name']}** updated.")

    @shop.command(name="itemdesc", usage="<section> | <item> | <text>")
    @commands.guild_only()
    @is_shop_staff()
    async def itemdesc(self, ctx: commands.Context, *, spec: str) -> None:
        """Set the small description shown under an item."""
        parts = split_spec(spec)
        if len(parts) < 3:
            raise commands.BadArgument("Use `itemdesc section | item | text`.")
        section = self._resolve_section(self._shop(ctx.guild), parts[0])
        item = self._resolve_item(section, parts[1])
        text = parts[2].strip()
        item["description"] = "" if text.lower() in {"none", "clear", "reset"} else truncate(text, 250)
        await self._finish(ctx, f"Description of **{item['name']}** updated.")

    @shop.command(name="itememoji", usage="<section> | <item> | <emoji>")
    @commands.guild_only()
    @is_shop_staff()
    async def itememoji(self, ctx: commands.Context, *, spec: str) -> None:
        """Add a small emoji next to an item's name."""
        parts = split_spec(spec)
        if len(parts) < 3:
            raise commands.BadArgument("Use `itememoji section | item | emoji`.")
        section = self._resolve_section(self._shop(ctx.guild), parts[0])
        item = self._resolve_item(section, parts[1])
        emoji = parts[2].strip()
        item["emoji"] = "" if emoji.lower() in {"none", "clear", "reset"} else truncate(emoji, 8)
        await self._finish(ctx, f"Emoji of **{item['name']}** updated.")

    @shop.command(name="renameitem", usage="<section> | <item> | <new name>")
    @commands.guild_only()
    @is_shop_staff()
    async def renameitem(self, ctx: commands.Context, *, spec: str) -> None:
        """Rename an item."""
        parts = split_spec(spec)
        if len(parts) < 3 or not parts[2]:
            raise commands.BadArgument("Use `renameitem section | item | new name`.")
        section = self._resolve_section(self._shop(ctx.guild), parts[0])
        item = self._resolve_item(section, parts[1])
        old = item["name"]
        item["name"] = truncate(parts[2], 100)
        await self._finish(ctx, f"Renamed **{old}** to **{item['name']}**.")

    # --------------------------------------------------------------- publish

    @shop.command(name="list")
    @commands.guild_only()
    @is_shop_staff()
    async def list_cmd(self, ctx: commands.Context) -> None:
        """List every section and item with numbers you can use in commands."""
        shop = self._shop(ctx.guild)
        if not shop["sections"]:
            await ctx.reply(
                embed=info_embed(f"No sections yet. Add one with `{ctx.prefix}shop addsection`."),
                mention_author=False,
            )
            return
        embed = info_embed(
            "Use these numbers with any shop command, or open a ticket to buy.",
            title="🛒 Shop contents",
        )
        for index, section in enumerate(shop["sections"], start=1):
            lines = [
                f"`{i}.` **{item['name']}** — {item['price']}"
                + (f" `{item['stock']}`" if item.get("stock") else "")
                for i, item in enumerate(section["items"], start=1)
            ]
            embed.add_field(
                name=f"{index}. {section.get('emoji', '')} {section['name']}".strip(),
                value=truncate("\n".join(lines) or "*No items yet.*", 1024),
                inline=False,
            )
        await ctx.reply(embed=embed, mention_author=False)

    @shop.command(name="preview")
    @commands.guild_only()
    @is_shop_staff()
    async def preview(self, ctx: commands.Context) -> None:
        """Preview the panel in this channel without publishing it."""
        shop = self._shop(ctx.guild)
        embeds, files = build_shop_embeds(shop)
        await ctx.reply(
            content="Preview — nothing here is saved as the live panel:",
            embeds=embeds,
            files=files or None,
            mention_author=False,
        )

    @shop.command(name="post", usage="[#channel]")
    @commands.guild_only()
    @is_shop_staff()
    async def post(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        """Publish the panel (replacing the previous one)."""
        shop = self._shop(ctx.guild)
        target = channel or ctx.guild.get_channel(shop.get("channel_id") or 0)
        if not isinstance(target, discord.TextChannel):
            raise commands.BadArgument(
                f"Choose a channel first: `{ctx.prefix}shop channel #channel`."
            )
        permissions = target.permissions_for(ctx.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            raise commands.BadArgument(
                f"I need **Send Messages** and **Embed Links** in {target.mention}."
            )

        await self._delete_panel(ctx.guild)
        try:
            embeds, files = build_shop_embeds(shop)
            message = await target.send(embeds=embeds, files=files or None)
        except discord.HTTPException as exc:
            raise commands.BadArgument(
                f"Discord rejected the panel ({exc}). Check your banner image URLs."
            ) from exc

        shop["channel_id"] = target.id
        shop["message_id"] = message.id
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🛒 Marketplace panel posted",
            description=f"The panel was published in {target.mention}.",
            color=0x2ECC71,
            fields=[
                ("By", ctx.author.mention, True),
                ("Sections", str(len(shop["sections"])), True),
            ],
        )
        await ctx.reply(
            embed=success_embed(
                f"Marketplace panel published in {target.mention}.\n"
                f"It will update automatically when you change sections or items.",
                title="🛒 Panel posted",
            ),
            mention_author=False,
        )

    @shop.command(name="reset")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def reset(self, ctx: commands.Context) -> None:
        """Delete the panel and erase every section and item."""
        if not await ConfirmView.prompt(
            ctx, "Delete the marketplace panel and reset **all** shop content?"
        ):
            return
        await self._delete_panel(ctx.guild)
        self.bot.db.guild(ctx.guild.id)["shop"] = deepcopy(DEFAULT_SHOP)
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🗑️ Marketplace reset",
            description="The panel and all shop content were deleted.",
            color=0xED4245,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed("Marketplace reset. Build it again whenever you like."),
            mention_author=False,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Marketplace(bot))
