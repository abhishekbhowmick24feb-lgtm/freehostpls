"""Ticket system: members open a private channel to buy a marketplace item."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
from typing import Optional

import discord
from discord.ext import commands

from core.agecheck import account_age_days, age_problem, min_age_days
from core.config import DATA_DIR
from core.media import (
    banner_change,
    banner_files,
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

TRANSCRIPT_MESSAGE_LIMIT = 2000
MAX_TRANSCRIPT_CHARS = 900_000


# ---------------------------------------------------------------- rendering


def find_shop_item(shop: dict, query: str) -> tuple[str, dict] | None:
    """Best-effort lookup of a marketplace item so we can show its price."""
    needle = query.strip().lower()
    if not needle:
        return None
    partial: tuple[str, dict] | None = None
    for section in shop.get("sections", []):
        for item in section.get("items", []):
            name = str(item.get("name", "")).lower()
            if name == needle:
                return section.get("name", "Shop"), item
            if partial is None and (needle in name or name in needle):
                partial = (section.get("name", "Shop"), item)
    return partial


def panel_embed(config: dict) -> discord.Embed:
    embed = discord.Embed(
        title=truncate(config.get("title") or "🎟️ Buy something", 256),
        description=truncate(
            (config.get("description") or "").replace("\\n", "\n"), 4000
        ) or None,
        color=parse_color(config.get("color")),
    )
    image = banner_url(config.get("image"))
    if image:
        embed.set_image(url=image)
    if config.get("thumbnail"):
        embed.set_thumbnail(url=config["thumbnail"])
    embed.set_footer(
        text=truncate(config.get("footer") or "Our staff will claim your ticket shortly", 2048)
    )
    return embed


def ticket_embed(record: dict, guild: discord.Guild) -> discord.Embed:
    buyer = guild.get_member(record.get("user_id") or 0)
    claimer = (
        guild.get_member(record["claimed_by"]) if record.get("claimed_by") else None
    )
    try:
        created = dt.datetime.fromisoformat(record["created_at"])
    except (KeyError, ValueError):
        created = discord.utils.utcnow()

    embed = discord.Embed(
        title=f"Ticket #{int(record.get('number', 0)):04d}",
        description="Thanks for your order! Staff will help you here.\n"
        "Use the buttons below to claim or close this ticket.",
        color=0x5865F2,
        timestamp=created,
    )
    embed.add_field(
        name="Buyer",
        value=buyer.mention if buyer else f"<@{record.get('user_id')}>",
        inline=True,
    )
    embed.add_field(name="Item", value=truncate(record.get("item", "—"), 1024), inline=True)
    embed.add_field(name="Quantity", value=f"`{record.get('quantity', 1)}`", inline=True)
    if record.get("price"):
        embed.add_field(
            name="Listed price",
            value=f"{truncate(str(record['price']), 100)} • *{truncate(record.get('section', 'Shop'), 100)}*",
            inline=True,
        )
    embed.add_field(
        name="Claimed by",
        value=claimer.mention if claimer else "*Not claimed yet*",
        inline=True,
    )
    if record.get("notes"):
        embed.add_field(
            name="Notes", value=truncate(str(record["notes"]), 1024), inline=False
        )
    embed.set_footer(text="Staff: claim this ticket, then close it when the deal is done.")
    return embed


def is_staff(member: discord.Member, config: dict) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_channels:
        return True
    role_id = config.get("staff_role_id")
    return bool(role_id and any(role.id == role_id for role in member.roles))


# -------------------------------------------------------------------- views


class TicketPanelView(discord.ui.View):
    """Persistent button that opens the purchase form."""

    def __init__(self, label: str = "Open a ticket") -> None:
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=truncate(label or "Open a ticket", 80),
            emoji="🎟️",
            style=discord.ButtonStyle.primary,
            custom_id="tickets:open",
        )
        button.callback = self._open
        self.add_item(button)

    async def _open(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Tickets")
        if cog is None:
            await interaction.response.send_message(
                embed=error_embed("The ticket system is not loaded right now."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(TicketModal(interaction.client))


class TicketActionsView(discord.ui.View):
    """Persistent claim/close buttons inside a ticket channel."""

    def __init__(self, claimed: bool = False) -> None:
        super().__init__(timeout=None)

        claim = discord.ui.Button(
            label="Claimed" if claimed else "Claim",
            emoji="🙋",
            style=discord.ButtonStyle.success,
            custom_id="tickets:claim",
            disabled=claimed,
        )
        close = discord.ui.Button(
            label="Close",
            emoji="🔒",
            style=discord.ButtonStyle.danger,
            custom_id="tickets:close",
        )
        claim.callback = self._claim
        close.callback = self._close
        self.add_item(claim)
        self.add_item(close)

    async def _claim(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Tickets")
        guild = interaction.guild
        if cog is None or guild is None:
            return
        config = cog.config(guild)
        record = config["open"].get(str(interaction.channel_id))
        if record is None:
            await interaction.response.send_message(
                embed=error_embed("This channel is not an open ticket."), ephemeral=True
            )
            return

        member = guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message(
                embed=error_embed("Could not find your member profile."), ephemeral=True
            )
            return
        if not is_staff(member, config):
            await interaction.response.send_message(
                embed=error_embed("Only staff can claim tickets."), ephemeral=True
            )
            return
        if record.get("claimed_by"):
            claimer = guild.get_member(record["claimed_by"])
            await interaction.response.send_message(
                embed=info_embed(
                    f"This ticket is already claimed by "
                    f"{claimer.mention if claimer else 'another staff member'}."
                ),
                ephemeral=True,
            )
            return

        record["claimed_by"] = member.id
        await cog.bot.db.save()
        await cog.bot.audit(
            guild,
            title="🙋 Ticket claimed",
            description=(
                f"**Ticket:** {interaction.channel.mention if interaction.channel else '—'}\n"
                f"**Staff:** {member.mention} (`{member.id}`)"
            ),
            color=0x57F287,
        )
        await interaction.response.send_message(
            embed=success_embed(f"{member.mention} is handling this ticket.")
        )
        try:
            if interaction.message is not None:
                await interaction.message.edit(
                    embed=ticket_embed(record, guild), view=TicketActionsView(claimed=True)
                )
        except discord.HTTPException:
            pass

    async def _close(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Tickets")
        guild = interaction.guild
        if cog is None or guild is None or interaction.channel is None:
            return
        config = cog.config(guild)
        record = config["open"].get(str(interaction.channel_id))
        if record is None:
            await interaction.response.send_message(
                embed=error_embed("This channel is not an open ticket."), ephemeral=True
            )
            return

        member = guild.get_member(interaction.user.id)
        if member is None:
            return
        if not (is_staff(member, config) or member.id == record.get("user_id")):
            await interaction.response.send_message(
                embed=error_embed("Only staff or the ticket opener can close this."),
                ephemeral=True,
            )
            return

        view = CloseConfirmView(member.id)
        await interaction.response.send_message(
            embed=info_embed("Close this ticket and delete the channel?"),
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if view.value:
            await cog.close_ticket(interaction.channel, member, config)


class CloseConfirmView(discord.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=60)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who pressed Close can confirm.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Close ticket", emoji="🔒", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = False
        await interaction.response.defer()
        self.stop()


class TicketModal(discord.ui.Modal):
    """Asks what the member is buying and how many."""

    def __init__(self, client: commands.Bot) -> None:
        super().__init__(title="Open a ticket", timeout=600)
        self.client = client

        self.item = discord.ui.TextInput(
            label="What are you buying?",
            placeholder="e.g. 1 Month Nitro Boost",
            max_length=200,
        )
        self.quantity = discord.ui.TextInput(
            label="How many?",
            placeholder="1",
            default="1",
            max_length=4,
        )
        self.notes = discord.ui.TextInput(
            label="Anything else? (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
            placeholder="Payment method, questions, etc.",
        )
        for field in (self.item, self.quantity, self.notes):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = self.client.get_cog("Tickets")
        if cog is None:
            await interaction.response.send_message(
                embed=error_embed("The ticket system is not loaded right now."),
                ephemeral=True,
            )
            return

        raw_quantity = self.quantity.value.strip() or "1"
        if not raw_quantity.isdigit() or int(raw_quantity) < 1:
            await interaction.response.send_message(
                embed=error_embed("**How many?** must be a whole number of 1 or more."),
                ephemeral=True,
            )
            return

        await cog.open_ticket(
            interaction,
            item=self.item.value.strip(),
            quantity=int(raw_quantity),
            notes=self.notes.value.strip() if self.notes.value else "",
        )


# --------------------------------------------------------------------- cog


class Tickets(commands.Cog):
    """Private purchase tickets with a panel, item form, claiming and closing."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def config(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)["tickets"]

    # --------------------------------------------------------------- helpers

    async def _delete_panel(self, guild: discord.Guild, config: dict) -> None:
        channel = guild.get_channel(config.get("channel_id") or 0)
        if not isinstance(channel, discord.TextChannel) or not config.get("message_id"):
            return
        try:
            message = await channel.fetch_message(config["message_id"])
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def _refresh_panel(self, guild: discord.Guild) -> bool:
        config = self.config(guild)
        channel = guild.get_channel(config.get("channel_id") or 0)
        if not isinstance(channel, discord.TextChannel) or not config.get("message_id"):
            return False
        try:
            message = await channel.fetch_message(config["message_id"])
            await message.edit(
                embed=panel_embed(config),
                view=self.panel_view(config),
                attachments=banner_files(config.get("image")),
            )
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    @staticmethod
    def panel_view(config: dict) -> TicketPanelView:
        return TicketPanelView(config.get("button_label") or "Open a ticket")

    async def _saved(self, ctx: commands.Context, text: str) -> None:
        await self.bot.db.save()
        if not self.config(ctx.guild).get("message_id"):
            suffix = f" Post the panel with `{ctx.prefix}ticket setup #channel`."
        elif await self._refresh_panel(ctx.guild):
            suffix = " The panel was updated."
        else:
            suffix = f" Run `{ctx.prefix}ticket refresh` to update the panel."
        await ctx.reply(embed=success_embed(text + suffix), mention_author=False)

    def transcript_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """Ticket transcript channel, falling back to the bot log channel."""
        config = self.config(guild)
        candidates = (
            config.get("ticket_log_channel_id"),
            self.bot.db.guild(guild.id).get("log_channel_id"),
        )
        for channel_id in candidates:
            channel = guild.get_channel(channel_id or 0)
            if isinstance(channel, discord.TextChannel):
                return channel
        return None

    async def build_transcript(
        self,
        channel: discord.TextChannel,
        record: dict,
        closer: discord.abc.User,
    ) -> tuple[str, int, bool]:
        """Return (text, message count, truncated)."""
        buyer_id = record.get("user_id")
        buyer = channel.guild.get_member(buyer_id or 0)
        buyer_label = str(buyer) if buyer else f"<@{buyer_id}>"
        claimer_id = record.get("claimed_by")
        claimer = channel.guild.get_member(claimer_id) if claimer_id else None
        claimer_label = (
            str(claimer) if claimer else (f"<@{claimer_id}>" if claimer_id else "unclaimed")
        )
        number = int(record.get("number", 0))
        price = record.get("price") or "not listed"
        if record.get("section"):
            price += f" ({record['section']})"

        header = "\n".join(
            [
                "Marketplace Guard — Ticket Transcript",
                "=" * 46,
                f"Guild: {channel.guild.name} ({channel.guild.id})",
                f"Ticket: #{number:04d} ({channel.name})",
                f"Buyer: {buyer_label} ({buyer_id})",
                f"Claimed by: {claimer_label}",
                f"Item: {record.get('item')}",
                f"Quantity: {record.get('quantity')}",
                f"Listed price: {price}",
                f"Notes: {record.get('notes') or '—'}",
                f"Opened: {record.get('created_at')}",
                f"Closed by: {closer} ({closer.id})",
                f"Closed at: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}",
                "=" * 46,
                "",
            ]
        )

        blocks: list[str] = [header]
        used = len(header)
        count = 0
        truncated = False

        async for message in channel.history(
            limit=TRANSCRIPT_MESSAGE_LIMIT, oldest_first=True
        ):
            count += 1
            stamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            lines = [
                f"[{stamp}] {message.author} ({message.author.id}): {message.clean_content}"
            ]
            for attachment in message.attachments:
                lines.append(f"    [attachment] {attachment.filename}: {attachment.url}")
            for embed in message.embeds:
                summary = embed.title or embed.description or ""
                lines.append(f"    [embed] {truncate(summary, 150)}")
            block = "\n".join(lines)

            if used + len(block) > MAX_TRANSCRIPT_CHARS:
                blocks.append(
                    f"\n… transcript truncated at {count} message(s) because of the size limit."
                )
                truncated = True
                break
            blocks.append(block)
            used += len(block) + 1

        blocks.append(f"\n--- End of transcript ({count} message(s)) ---")
        return "\n".join(blocks), count, truncated

    async def send_transcript(
        self,
        channel: discord.TextChannel,
        record: dict,
        closer: discord.abc.User,
    ) -> None:
        """Save the transcript locally and upload it to the staff channel."""
        guild = channel.guild
        number = int(record.get("number", 0))
        text, count, truncated = await self.build_transcript(channel, record, closer)

        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        folder = DATA_DIR / "transcripts" / str(guild.id)
        path = folder / f"ticket-{number:04d}-{stamp}.txt"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, text, encoding="utf-8")
        except OSError as exc:
            log.warning("Could not save transcript to disk: %s", exc)

        target = self.transcript_channel(guild)
        if target is None:
            return

        embed = discord.Embed(
            title=f"🎟️ Ticket #{number:04d} transcript",
            description=(
                f"**Buyer:** <@{record.get('user_id')}>\n"
                f"**Item:** {truncate(record.get('item', '—'), 200)}\n"
                f"**Quantity:** {record.get('quantity')}\n"
                f"**Closed by:** {closer.mention}"
            ),
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Messages", value=f"`{count}`", inline=True)
        embed.add_field(
            name="Listed price", value=truncate(record.get("price") or "not listed", 100), inline=True
        )
        if record.get("claimed_by"):
            embed.add_field(
                name="Handled by", value=f"<@{record['claimed_by']}>", inline=True
            )
        if truncated:
            embed.set_footer(text="Transcript was shortened because of the size limit.")

        try:
            await target.send(
                embed=embed,
                file=discord.File(
                    io.StringIO(text), filename=f"ticket-{number:04d}.txt"
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            log.warning("Could not post transcript for %s: %s", channel.id, exc)

    async def close_ticket(
        self,
        channel: discord.TextChannel,
        closer: discord.abc.User,
        config: dict,
    ) -> None:
        record = config["open"].get(str(channel.id))
        if record is not None:
            await self.send_transcript(channel, record, closer)
        config["open"].pop(str(channel.id), None)
        await self.bot.db.save()
        await self.bot.audit(
            channel.guild,
            title="🔒 Ticket closed",
            description=(
                f"**Ticket:** {channel.name}\n"
                f"**Closed by:** {closer.mention} (`{closer.id}`)"
                + (f"\n**Buyer:** <@{record['user_id']}>" if record else "")
            ),
            color=0xED4245,
        )
        try:
            await channel.send(
                embed=info_embed(
                    f"Ticket closed by **{closer}**. Deleting this channel in 5 seconds…"
                )
            )
        except discord.HTTPException:
            pass
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket closed by {closer} ({closer.id})")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not delete ticket channel %s: %s", channel.id, exc)

    # ------------------------------------------------------------- open flow

    async def open_ticket(
        self,
        interaction: discord.Interaction,
        *,
        item: str,
        quantity: int,
        notes: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=error_embed("Tickets only work inside a server."), ephemeral=True
            )
            return

        member = guild.get_member(interaction.user.id)
        if member is None:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except discord.HTTPException:
                await interaction.response.send_message(
                    embed=error_embed("I could not find you in this server."),
                    ephemeral=True,
                )
                return

        config = self.config(guild)

        minimum = min_age_days(self.bot, guild)
        problem = age_problem(member, minimum)
        if problem:
            await interaction.response.send_message(
                embed=error_embed(
                    problem
                    + "\n\nYou cannot open a purchase ticket until your "
                    "account meets the minimum age."
                ),
                ephemeral=True,
            )
            await self.bot.audit(
                guild,
                title="🕒 Ticket blocked — account too young",
                description=(
                    f"{member.mention} tried to open a purchase ticket but "
                    "does not meet the account-age rule."
                ),
                color=0xFEE75C,
                fields=[
                    ("Account age", f"{account_age_days(member):g} days", True),
                    ("Minimum", f"{minimum} days", True),
                ],
            )
            return

        # Clean up stale records and stop duplicate tickets.
        for channel_id, record in list(config["open"].items()):
            existing = guild.get_channel(int(channel_id))
            if existing is None:
                config["open"].pop(channel_id, None)
            elif record.get("user_id") == member.id:
                await self.bot.db.save()
                await interaction.response.send_message(
                    embed=error_embed(
                        f"You already have an open ticket: {existing.mention}"
                    ),
                    ephemeral=True,
                )
                return

        if not guild.me.guild_permissions.manage_channels:
            await interaction.response.send_message(
                embed=error_embed("I need the **Manage Channels** permission to open tickets."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        config["counter"] = int(config.get("counter", 0)) + 1
        number = config["counter"]
        name = f"ticket-{number:04d}"

        category = guild.get_channel(config.get("category_id") or 0)
        if not isinstance(category, discord.CategoryChannel):
            category = None

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                embed_links=True,
                attach_files=True,
            ),
        }
        staff_role = guild.get_role(config.get("staff_role_id") or 0)
        if staff_role is not None:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                embed_links=True,
                attach_files=True,
            )

        try:
            channel = await guild.create_text_channel(
                name,
                category=category,
                overwrites=overwrites,
                topic=truncate(
                    f"Buyer: {member} ({member.id}) • {quantity}x {item}", 1024
                ),
                reason=f"Ticket opened by {member} ({member.id})",
            )
        except discord.HTTPException as exc:
            log.warning("Could not create ticket channel: %s", exc)
            await interaction.followup.send(
                embed=error_embed(
                    "I could not create your ticket channel. Please tell the staff."
                ),
                ephemeral=True,
            )
            return

        record: dict = {
            "number": number,
            "user_id": member.id,
            "item": item,
            "quantity": quantity,
            "notes": notes,
            "claimed_by": None,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "price": None,
            "section": None,
        }

        match = find_shop_item(self.bot.db.guild(guild.id)["shop"], item)
        if match is not None:
            record["section"], shop_item = match
            record["price"] = shop_item.get("price")

        config["open"][str(channel.id)] = record
        await self.bot.db.save()

        try:
            ping = f"{member.mention} — your ticket is ready!"
            if staff_role is not None:
                ping += f" {staff_role.mention}"
            message = await channel.send(
                content=ping,
                embed=ticket_embed(record, guild),
                view=TicketActionsView(),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=True, users=True
                ),
            )
            record["message_id"] = message.id
            await self.bot.db.save()
        except discord.HTTPException:
            pass

        await self.bot.audit(
            guild,
            title="🎟️ Ticket opened",
            description=(
                f"**Buyer:** {member.mention} (`{member.id}`)\n"
                f"**Item:** {truncate(item, 200)}\n"
                f"**Quantity:** {quantity}\n"
                f"**Channel:** {channel.mention}"
            ),
            color=0x2ECC71,
            fields=[
                ("Listed price", record.get("price") or "not listed", True),
                ("Ticket", f"#{number:04d}", True),
            ],
        )
        await interaction.followup.send(
            embed=success_embed(
                f"Your ticket is open: {channel.mention}\n"
                f"**Item:** {item}\n**Quantity:** {quantity}",
                title="🎟️ Ticket created",
            ),
            ephemeral=True,
        )

    # ---------------------------------------------------------------- group

    @commands.hybrid_group(
        name="ticket",
        aliases=["tickets", "support"],
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def ticket(self, ctx: commands.Context) -> None:
        """Buy-item tickets: panel, private channels, claiming and closing."""
        config = self.config(ctx.guild)
        channel = ctx.guild.get_channel(config.get("channel_id") or 0)
        category = ctx.guild.get_channel(config.get("category_id") or 0)
        role = ctx.guild.get_role(config.get("staff_role_id") or 0)
        transcript = ctx.guild.get_channel(config.get("ticket_log_channel_id") or 0)
        open_count = sum(
            1 for cid in config["open"] if ctx.guild.get_channel(int(cid)) is not None
        )
        embed = info_embed(
            f"**Panel channel:** {channel.mention if channel else 'not set'}\n"
            f"**Panel:** {'posted' if config.get('message_id') else 'not posted'}\n"
            f"**Category:** {category.name if category else 'default'}\n"
            f"**Staff role:** {role.mention if role else 'not set'}\n"
            f"**Transcripts:** {transcript.mention if transcript else 'bot log channel (fallback)'}\n"
            f"**Open tickets:** `{open_count}`\n\n"
            f"**Setup:** `{ctx.prefix}ticket setup #channel [@staff]`\n"
            f"**Look:** `{ctx.prefix}ticket title/description/image/thumbnail/color/button`\n"
            f"**Close:** use the button in a ticket, or `{ctx.prefix}ticket close`.",
            title="🎟️ Tickets",
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ---------------------------------------------------------------- setup

    @ticket.command(name="setup", usage="<#channel> [@staff]")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def setup(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        staff_role: discord.Role | None = None,
    ) -> None:
        """Post the ticket panel in a channel and optionally set the staff role."""
        permissions = channel.permissions_for(ctx.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            await ctx.reply(
                embed=error_embed(
                    f"I need **Send Messages** and **Embed Links** in {channel.mention}."
                ),
                mention_author=False,
            )
            return
        if staff_role is not None and staff_role.is_default():
            await ctx.reply(
                embed=error_embed("Pick a real staff role, not @everyone."),
                mention_author=False,
            )
            return

        config = self.config(ctx.guild)
        await self._delete_panel(ctx.guild, config)

        config["channel_id"] = channel.id
        if staff_role is not None:
            config["staff_role_id"] = staff_role.id

        try:
            message = await channel.send(
                embed=panel_embed(config),
                view=self.panel_view(config),
                files=banner_files(config.get("image")),
            )
        except discord.HTTPException as exc:
            await ctx.reply(
                embed=error_embed(f"I could not post the panel: {exc}"),
                mention_author=False,
            )
            return

        config["message_id"] = message.id
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(
                f"Ticket panel posted in {channel.mention}."
                + (
                    f" Staff role: {staff_role.mention}."
                    if staff_role is not None
                    else " Set one with `ticket role @Staff` so staff can claim."
                ),
                title="🎟️ Tickets ready",
            ),
            mention_author=False,
        )

    # ------------------------------------------------------------ settings

    @ticket.command(name="category", usage="<#category|none>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def category_cmd(self, ctx: commands.Context, *, value: str) -> None:
        """Choose the category new ticket channels are created in."""
        config = self.config(ctx.guild)
        if value.lower() in {"none", "clear", "reset"}:
            config["category_id"] = None
            await self._saved(ctx, "Tickets will be created without a category.")
            return
        try:
            category = await commands.CategoryChannelConverter().convert(ctx, value)
        except commands.BadArgument as exc:
            raise commands.BadArgument(
                "Mention a category, e.g. `ticket category #Tickets`."
            ) from exc
        config["category_id"] = category.id
        await self._saved(ctx, f"Ticket channels will be created in **{category.name}**.")

    @ticket.command(name="role", usage="<@role|none>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def role_cmd(self, ctx: commands.Context, *, value: str) -> None:
        """Set the staff role that can see, claim and close tickets."""
        config = self.config(ctx.guild)
        if value.lower() in {"none", "clear", "reset"}:
            config["staff_role_id"] = None
            await self._saved(ctx, "Staff role cleared. Managers can still claim tickets.")
            return
        try:
            role = await commands.RoleConverter().convert(ctx, value)
        except commands.BadArgument as exc:
            raise commands.BadArgument("Mention a role, e.g. `ticket role @Staff`.") from exc
        if role.is_default() or role.managed:
            raise commands.BadArgument("That role cannot be used for tickets.")
        if role >= ctx.guild.me.top_role:
            raise commands.BadArgument("That role is higher than my highest role.")
        config["staff_role_id"] = role.id
        await self._saved(ctx, f"Staff role set to {role.mention}.")

    @ticket.command(name="log", usage="<#channel|none>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def log_cmd(self, ctx: commands.Context, *, value: str) -> None:
        """Choose where closed-ticket transcripts are posted."""
        config = self.config(ctx.guild)
        if value.lower() in {"none", "clear", "reset"}:
            config["ticket_log_channel_id"] = None
            await self.bot.db.save()
            await ctx.reply(
                embed=success_embed(
                    "Transcripts will go to the bot log channel if one is set "
                    f"(`{ctx.prefix}logs channel #channel`)."
                ),
                mention_author=False,
            )
            return
        try:
            channel = await commands.TextChannelConverter().convert(ctx, value)
        except commands.BadArgument as exc:
            raise commands.BadArgument(
                "Mention a channel, e.g. `ticket log #ticket-logs`."
            ) from exc
        permissions = channel.permissions_for(ctx.guild.me)
        if not (
            permissions.send_messages
            and permissions.embed_links
            and permissions.attach_files
        ):
            raise commands.BadArgument(
                f"I need **Send Messages**, **Embed Links** and **Attach Files** "
                f"in {channel.mention}."
            )
        config["ticket_log_channel_id"] = channel.id
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🎟️ Transcript channel set",
            description=f"Closed-ticket transcripts will be posted in {channel.mention}.",
            color=0x2ECC71,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"Ticket transcripts will be posted in {channel.mention}."
            ),
            mention_author=False,
        )

    @ticket.command(name="title", usage="<text>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def title_cmd(self, ctx: commands.Context, *, text: str) -> None:
        """Change the panel title."""
        self.config(ctx.guild)["title"] = truncate(text, 256)
        await self._saved(ctx, f"Title set to **{truncate(text, 100)}**.")

    @ticket.command(name="description", usage="<text>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def description_cmd(self, ctx: commands.Context, *, text: str) -> None:
        """Change the panel text. Use \\n for a new line."""
        config = self.config(ctx.guild)
        config["description"] = (
            "" if text.lower() in {"none", "clear", "reset"} else truncate(text, 4000)
        )
        await self._saved(ctx, "Panel description updated.")

    @ticket.command(name="image", usage="<url|none>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def image_cmd(
        self,
        ctx: commands.Context,
        *,
        url: str | None = None,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Add or remove the panel banner image / GIF."""
        config = self.config(ctx.guild)
        value, error = await banner_from_input(ctx, attachment, url)
        if error is not None:
            raise commands.BadArgument(error)
        config["image"] = value
        await self._saved(ctx, f"Panel image {banner_change(value)}.")

    @ticket.command(name="thumbnail", usage="<url|none>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def thumbnail_cmd(self, ctx: commands.Context, *, url: str) -> None:
        """Add or remove the panel thumbnail."""
        config = self.config(ctx.guild)
        if url.lower() in {"none", "clear", "reset"}:
            config["thumbnail"] = ""
            await self._saved(ctx, "Thumbnail removed.")
            return
        if not url.startswith(("http://", "https://")):
            raise commands.BadArgument("That does not look like an image URL.")
        config["thumbnail"] = url.strip()
        await self._saved(ctx, "Thumbnail updated.")

    @ticket.command(name="footer", usage="<text>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def footer_cmd(self, ctx: commands.Context, *, text: str) -> None:
        """Change the panel footer."""
        config = self.config(ctx.guild)
        config["footer"] = (
            "" if text.lower() in {"none", "clear", "reset"} else truncate(text, 2048)
        )
        await self._saved(ctx, "Footer updated.")

    @ticket.command(name="color", usage="<#hex>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def color_cmd(self, ctx: commands.Context, hex_code: str) -> None:
        """Change the panel colour, e.g. #5865F2."""
        cleaned = hex_code.strip().lstrip("#")
        if len(cleaned) != 6:
            raise commands.BadArgument("Use a 6 character hex colour, like `#5865F2`.")
        try:
            int(cleaned, 16)
        except ValueError as exc:
            raise commands.BadArgument("That is not a valid hex colour.") from exc
        self.config(ctx.guild)["color"] = f"#{cleaned}"
        await self._saved(ctx, f"Panel colour set to `#{cleaned}`.")

    @ticket.command(name="button", usage="<label>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def button_cmd(self, ctx: commands.Context, *, label: str) -> None:
        """Change the text on the panel button."""
        self.config(ctx.guild)["button_label"] = truncate(label, 80)
        await self._saved(ctx, f"Button label set to **{truncate(label, 80)}**.")

    @ticket.command(name="refresh", aliases=["repost"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def refresh(self, ctx: commands.Context) -> None:
        """Re-render the panel with the current settings."""
        config = self.config(ctx.guild)
        if not config.get("message_id"):
            raise commands.BadArgument(
                f"No panel exists yet. Run `{ctx.prefix}ticket setup #channel`."
            )
        if await self._refresh_panel(ctx.guild):
            await ctx.reply(
                embed=success_embed("Ticket panel refreshed."), mention_author=False
            )
        else:
            raise commands.BadArgument(
                "I could not find the panel message — it was probably deleted. "
                f"Run `{ctx.prefix}ticket setup` again."
            )

    @ticket.command(name="close")
    @commands.guild_only()
    async def close_cmd(self, ctx: commands.Context) -> None:
        """Close the ticket in this channel (staff or the opener)."""
        config = self.config(ctx.guild)
        record = config["open"].get(str(ctx.channel.id))
        if record is None:
            raise commands.BadArgument("This channel is not an open ticket.")
        if not (is_staff(ctx.author, config) or ctx.author.id == record.get("user_id")):
            raise commands.BadArgument("Only staff or the ticket opener can close this.")
        if not await ConfirmView.prompt(
            ctx, "Close this ticket and delete the channel?", confirm_label="Close ticket"
        ):
            return
        await self.close_ticket(ctx.channel, ctx.author, config)

    @ticket.command(name="status")
    @commands.guild_only()
    async def status(self, ctx: commands.Context) -> None:
        """Show the current ticket settings."""
        await self.ticket(ctx)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
