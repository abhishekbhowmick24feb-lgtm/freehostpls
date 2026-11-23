"""Modmail: members DM the bot, staff reply from a private channel."""

from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord.ext import commands

from cogs.moderation import is_moderator
from core.config import DEFAULT_PREFIX, INFO_COLOR, SUCCESS_COLOR
from core.utils import (
    error_embed,
    info_embed,
    success_embed,
    truncate,
)

log = logging.getLogger(__name__)

MODMAIL_COLOR = 0x5865F2


class UserCloseView(discord.ui.View):
    """Ask a member in DMs to confirm closing their own ticket."""

    def __init__(self, reason: str) -> None:
        super().__init__(timeout=300)
        self.reason = reason

    @discord.ui.button(
        label="Yes, close it", emoji="🔒", style=discord.ButtonStyle.danger
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        cog = interaction.client.get_cog("Modmail")
        if cog is None:
            await interaction.response.send_message(
                embed=error_embed("Modmail is not loaded right now."),
                ephemeral=True,
            )
            return
        await cog.finish_user_close(interaction, self.reason)

    @discord.ui.button(
        label="No, keep it open", style=discord.ButtonStyle.secondary
    )
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=info_embed(
                "Your ticket is still open — just reply here to keep talking "
                "to staff.",
                title="📬 Ticket kept open",
            ),
            view=self,
        )
        self.stop()


class StaffClosedView(discord.ui.View):
    """Let staff delete the channel after the member closed the ticket."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close channel",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id="modmail:close_closed_ticket",
    )
    async def close_channel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        guild = interaction.guild
        cog = interaction.client.get_cog("Modmail")
        member = guild.get_member(interaction.user.id) if guild else None
        if (
            guild is None
            or cog is None
            or member is None
            or not cog._is_staff(member, guild)
        ):
            await interaction.response.send_message(
                embed=error_embed("Only staff can close this channel."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed("Closing this channel in 3 seconds…")
        )
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(
                reason=(
                    f"Modmail channel closed by {interaction.user} "
                    f"({interaction.user.id})"
                )
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not delete modmail channel: %s", exc)


class Modmail(commands.Cog):
    """Relay member DMs into private staff channels and back."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        # Persistent so the staff "Close channel" button keeps working
        # after a restart.
        self.bot.add_view(StaffClosedView())

    # --------------------------------------------------------------- helpers

    def _prefixes(self, guild: discord.Guild) -> list[str]:
        configured = self._data(guild).get("prefix") or DEFAULT_PREFIX
        prefixes = [configured]
        for extra in ("?", "!"):
            if extra not in prefixes:
                prefixes.append(extra)
        return prefixes

    @staticmethod
    def _parse_command(
        content: str, prefixes: list[str]
    ) -> tuple[str, str] | None:
        text = (content or "").strip()
        for prefix in prefixes:
            if prefix and text.lower().startswith(prefix.lower()):
                rest = text[len(prefix) :].strip()
                if not rest:
                    return "", ""
                name, _, args = rest.partition(" ")
                return name.strip().lower(), args.strip()
        return None

    async def _safe_dm(self, user: discord.abc.User, **kwargs) -> bool:
        try:
            await user.send(**kwargs)
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.info("Could not DM %s: %s", getattr(user, "id", "?"), exc)
            return False

    def _data(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)

    def _category(self, guild: discord.Guild) -> discord.CategoryChannel | None:
        channel_id = self._data(guild).get("modmail_category_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.CategoryChannel) else None

    def _staff_role(self, guild: discord.Guild) -> discord.Role | None:
        data = self._data(guild)
        role_id = data.get("modmail_staff_role_id") or data.get("mod_role_id")
        return guild.get_role(role_id) if role_id else None

    def _is_staff(self, member: discord.Member, guild: discord.Guild) -> bool:
        if member.guild_permissions.administrator:
            return True
        if member.guild_permissions.manage_messages:
            return True
        role = self._staff_role(guild)
        if role is not None and role in member.roles:
            return True
        mod_role_id = self._data(guild).get("mod_role_id")
        mod_role = guild.get_role(mod_role_id) if mod_role_id else None
        return bool(mod_role is not None and mod_role in member.roles)

    def _channel_for_user(
        self, guild: discord.Guild, user_id: int
    ) -> discord.TextChannel | None:
        channel_id = self._data(guild).get("modmail_open", {}).get(str(user_id))
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    def _is_modmail_channel(
        self, guild: discord.Guild, channel: discord.abc.GuildChannel
    ) -> bool:
        if not isinstance(channel, discord.TextChannel):
            return False
        if (channel.topic or "").startswith("Modmail with "):
            return True
        category = self._category(guild)
        return category is not None and channel.category_id == category.id

    def _guild_for_user(self, user: discord.abc.User) -> discord.Guild | None:
        configured = [
            guild
            for guild in self.bot.guilds
            if self._data(guild).get("modmail_category_id")
        ]
        if not configured:
            return None
        member_guilds = [guild for guild in configured if guild.get_member(user.id)]
        return member_guilds[0] if member_guilds else configured[0]

    async def _files(
        self, attachments: list[discord.Attachment]
    ) -> list[discord.File]:
        files: list[discord.File] = []
        for attachment in attachments[:10]:
            try:
                files.append(await attachment.to_file())
            except (discord.HTTPException, OSError) as exc:
                log.warning("Could not forward attachment %s: %s", attachment.filename, exc)
        return files

    async def _new_channel(
        self, guild: discord.Guild, user: discord.abc.User
    ) -> discord.TextChannel | None:
        category = self._category(guild)
        staff_role = self._staff_role(guild)
        me = guild.me
        if category is None or staff_role is None or me is None:
            return None
        permissions = category.permissions_for(me)
        if not (permissions.view_channel and permissions.manage_channels):
            return None

        base = re.sub(r"[^a-z0-9-]+", "-", user.name.lower()).strip("-")
        base = base[:40] or "user"
        name = f"modmail-{base}-{user.id}"[:100]

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                embed_links=True,
                attach_files=True,
            ),
            staff_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                embed_links=True,
                attach_files=True,
            ),
        }

        try:
            channel = await guild.create_text_channel(
                name,
                category=category,
                overwrites=overwrites,
                topic=f"Modmail with {user} ({user.id})",
                reason=f"Modmail opened by {user} ({user.id})",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not create modmail channel for %s: %s", user.id, exc)
            return None

        data = self._data(guild)
        data["modmail_counter"] = int(data.get("modmail_counter", 0)) + 1
        data.setdefault("modmail_open", {})[str(user.id)] = channel.id
        await self.bot.db.save()

        embed = discord.Embed(
            title="📬 New modmail",
            description=(
                f"**User:** {user} (`{user.id}`)\n"
                f"**Account created:** "
                f"{discord.utils.format_dt(user.created_at, 'R')}\n\n"
                "Reply in this channel to DM the user. Staff replies are sent "
                "as **Staff**, so your name is not shown.\n"
                "The member can close the ticket from DMs; use "
                "`/modmail close` when the conversation is finished."
            ),
            color=INFO_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        try:
            await channel.send(
                content=staff_role.mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=True, users=False
                ),
            )
        except discord.HTTPException:
            pass
        return channel

    async def _post_to_channel(
        self,
        channel: discord.TextChannel,
        author: discord.abc.User,
        content: str,
        attachments: list[discord.Attachment],
        created_at: object | None = None,
    ) -> None:
        embed = discord.Embed(
            description=truncate(content or "*(no text)*", 4000),
            color=MODMAIL_COLOR,
            timestamp=created_at or discord.utils.utcnow(),
        )
        embed.set_author(
            name=str(author),
            icon_url=author.display_avatar.url,
        )
        embed.set_footer(text=f"User ID: {author.id}")
        if attachments:
            links = "\n".join(
                f"[{attachment.filename}]({attachment.url})"
                for attachment in attachments[:10]
            )
            embed.add_field(
                name="Attachments", value=truncate(links, 1024), inline=False
            )

        files = await self._files(attachments)
        kwargs: dict = {
            "embed": embed,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if files:
            kwargs["files"] = files
        await channel.send(**kwargs)

    async def _relay_to_channel(
        self, channel: discord.TextChannel, message: discord.Message
    ) -> None:
        await self._post_to_channel(
            channel,
            message.author,
            message.content,
            message.attachments,
            message.created_at,
        )

    async def _send_to_user(
        self,
        user: discord.abc.User,
        content: str,
        *,
        attachments: list[discord.Attachment] | None = None,
    ) -> bool:
        embed = discord.Embed(
            description=truncate(content or "*(no text)*", 4000),
            color=MODMAIL_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="Staff")
        embed.set_footer(
            text="Reply here to continue the conversation."
        )
        if attachments:
            links = "\n".join(
                f"[{attachment.filename}]({attachment.url})"
                for attachment in attachments[:10]
            )
            embed.add_field(
                name="Attachments", value=truncate(links, 1024), inline=False
            )

        files = await self._files(attachments or [])
        kwargs: dict = {"embed": embed}
        if files:
            kwargs["files"] = files
        try:
            await user.send(**kwargs)
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.info("Could not DM modmail reply to %s: %s", user.id, exc)
            return False

    # --------------------------------------------------------------- events

    async def _send_open_instructions(
        self, user: discord.abc.User, prefix: str
    ) -> None:
        await self._safe_dm(
            user,
            embed=info_embed(
                "Staff do not see your DMs until you open a ticket.\n\n"
                f"**Type `{prefix}open`** to open a modmail ticket.\n"
                f"**Type `{prefix}close`** to close a ticket you already "
                "opened.\n\n"
                "Nothing is sent to staff unless you open a ticket.",
                title="📬 Contact staff",
            ),
        )

    async def _handle_dm(self, message: discord.Message) -> None:
        user = message.author
        guild = self._guild_for_user(user)
        if guild is None:
            await self._safe_dm(
                user,
                "Staff modmail is not set up right now. Please try again later.",
            )
            return

        prefixes = self._prefixes(guild)
        command = self._parse_command(message.content or "", prefixes)
        if command is not None:
            name, args = command
            if name == "open":
                await self._open_ticket(user, guild, args)
                return
            if name == "close":
                await self._request_close(user, guild, args)
                return

        lock = self._locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            channel = self._channel_for_user(guild, user.id)
            if channel is not None:
                try:
                    await self._relay_to_channel(channel, message)
                    return
                except discord.NotFound:
                    data = self._data(guild)
                    data.get("modmail_open", {}).pop(str(user.id), None)
                    await self.bot.db.save()
                except discord.HTTPException as exc:
                    log.warning(
                        "Could not relay modmail from %s: %s", user.id, exc
                    )
                    return

            await self._send_open_instructions(user, prefixes[0])

    async def _open_ticket(
        self, user: discord.abc.User, guild: discord.Guild, first_message: str
    ) -> None:
        prefix = self._prefixes(guild)[0]
        lock = self._locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            channel = self._channel_for_user(guild, user.id)
            if channel is not None:
                if first_message:
                    try:
                        await self._post_to_channel(
                            channel, user, first_message, []
                        )
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        log.warning(
                            "Could not relay modmail from %s: %s", user.id, exc
                        )
                await self._safe_dm(
                    user,
                    embed=info_embed(
                        "You already have an open modmail ticket. Reply here "
                        "to keep talking to staff.\n"
                        f"Type `{prefix}close` to close it.",
                        title="📬 Ticket already open",
                    ),
                )
                return

            channel = await self._new_channel(guild, user)
            if channel is None:
                await self._safe_dm(
                    user,
                    embed=error_embed(
                        "I could not open a staff conversation. Please tell an "
                        "administrator that modmail needs checking."
                    ),
                )
                return
            if first_message:
                try:
                    await self._post_to_channel(channel, user, first_message, [])
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning(
                        "Could not relay modmail from %s: %s", user.id, exc
                    )

        await self._safe_dm(
            user,
            embed=info_embed(
                "Your ticket is open and your message was sent to staff.\n"
                "Replies will arrive here.\n\n"
                f"Type `{prefix}close` whenever you want to close it.",
                title="📬 Modmail opened",
            ),
        )

    async def _request_close(
        self, user: discord.abc.User, guild: discord.Guild, reason: str
    ) -> None:
        channel = self._channel_for_user(guild, user.id)
        if channel is None:
            await self._safe_dm(
                user,
                embed=info_embed(
                    "You do not have an open modmail ticket.\n"
                    f"Type `{self._prefixes(guild)[0]}open` to start one.",
                    title="📬 No open ticket",
                ),
            )
            return

        text = (
            "**Are you sure you want to close your modmail ticket?**\n\n"
            "Staff will be told it was closed by you and you will not get "
            "more replies here."
        )
        if reason:
            text += f"\n\n**Your reason:** {truncate(reason, 500)}"
        await self._safe_dm(
            user,
            embed=info_embed(text, title="📬 Close this ticket?"),
            view=UserCloseView(reason),
        )

    async def finish_user_close(
        self, interaction: discord.Interaction, reason: str
    ) -> None:
        """Close the member's ticket after they confirm in DMs."""
        user = interaction.user
        guild = self._guild_for_user(user)
        channel = (
            self._channel_for_user(guild, user.id)
            if guild is not None
            else None
        )
        if guild is None or channel is None:
            await interaction.response.edit_message(
                embed=info_embed(
                    "This ticket is already closed.", title="📬 Ticket closed"
                ),
                view=None,
            )
            return

        data = self._data(guild)
        data.get("modmail_open", {}).pop(str(user.id), None)
        await self.bot.db.save()

        await interaction.response.edit_message(
            embed=info_embed(
                "Your modmail ticket is closed.\n"
                f"Type `{self._prefixes(guild)[0]}open` to start a new one.",
                title="📬 Ticket closed",
            ),
            view=None,
        )

        embed = discord.Embed(
            title="📪 Ticket closed by the member",
            description=(
                f"**User:** {user} (`{user.id}`)\n"
                f"**Reason:** {truncate(reason or 'No reason given', 1000)}\n\n"
                "Replies to this user are disabled now. Staff can only close "
                "this channel."
            ),
            color=INFO_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        try:
            await channel.send(
                embed=embed,
                view=StaffClosedView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not announce member-closed modmail: %s", exc)

        await self.bot.audit(
            guild,
            title="📬 Modmail closed by member",
            description=(
                f"**User:** <@{user.id}> (`{user.id}`)\n"
                f"**Channel:** {channel.mention}\n"
                f"**Reason:** {truncate(reason or 'No reason given', 500)}"
            ),
            color=INFO_COLOR,
        )

    async def _handle_staff_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None:
            return
        data = self._data(guild)
        user_id: int | None = None
        for raw_id, raw_channel in data.get("modmail_open", {}).items():
            try:
                if int(raw_channel) == message.channel.id:
                    user_id = int(raw_id)
                    break
            except (TypeError, ValueError):
                continue
        if user_id is None:
            return

        member = message.author
        if not isinstance(member, discord.Member) or not self._is_staff(member, guild):
            return

        prefixes = await self.bot.get_prefix(message)
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        if any(
            prefix and message.content.startswith(prefix) for prefix in prefixes
        ):
            return

        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                await message.channel.send(
                    embed=error_embed(
                        "I could not find that user. Close this modmail and ask "
                        "them to DM again."
                    )
                )
                return

        delivered = await self._send_to_user(
            user,
            message.content,
            attachments=message.attachments,
        )
        if delivered:
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass
            return

        try:
            await message.add_reaction("❌")
        except discord.HTTPException:
            pass
        await message.channel.send(
            embed=error_embed(
                "I could not DM that user — they may have DMs closed. "
                "Ask them to open their privacy settings."
            )
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            await self._handle_dm(message)
        else:
            await self._handle_staff_message(message)

    # --------------------------------------------------------------- commands

    def _status_embed(self, guild: discord.Guild) -> discord.Embed:
        data = self._data(guild)
        category = self._category(guild)
        role = self._staff_role(guild)
        open_count = 0
        for raw_channel in data.get("modmail_open", {}).values():
            try:
                if guild.get_channel(int(raw_channel)) is not None:
                    open_count += 1
            except (TypeError, ValueError):
                continue
        return info_embed(
            f"**Category:** {category.mention if category else 'not set'}\n"
            f"**Staff role:** {role.mention if role else 'not set'}\n"
            f"**Open conversations:** `{open_count}`\n\n"
            "Members start a ticket with `?open` in DMs — other DMs only get "
            "instructions. Staff reply by typing in the private modmail "
            "channel; replies are sent as **Staff**.\n"
            "Members close their own ticket with `?close` and staff see the "
            "reason in the modmail channel.\n\n"
            "**Setup:** `/modmail setup #category @role`\n"
            f"**Close:** `/modmail close [reason]` in a modmail channel.\n"
            f"**Reply:** type in the channel, or `/modmail reply <message>`.",
            title="📬 Modmail",
        )

    @commands.hybrid_group(
        name="modmail",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def modmail(self, ctx: commands.Context) -> None:
        """Open and manage DM-based staff conversations."""
        await ctx.reply(
            embed=self._status_embed(ctx.guild), mention_author=False
        )

    @modmail.command(name="setup", usage="<#category> [@staff]")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def setup(
        self,
        ctx: commands.Context,
        category: discord.CategoryChannel,
        staff_role: discord.Role | None = None,
    ) -> None:
        """Set the private category and staff role used by modmail."""
        staff_role = staff_role or self._staff_role(ctx.guild)
        if staff_role is None:
            await ctx.reply(
                embed=error_embed(
                    "Choose a staff role, or set a moderator role first with "
                    "`/modrole`."
                ),
                mention_author=False,
            )
            return
        if staff_role.is_default() or staff_role.managed:
            await ctx.reply(
                embed=error_embed("That role cannot be used for modmail staff."),
                mention_author=False,
            )
            return
        if staff_role >= ctx.guild.me.top_role:
            await ctx.reply(
                embed=error_embed(
                    "My highest role must be above the modmail staff role."
                ),
                mention_author=False,
            )
            return
        if not category.permissions_for(ctx.guild.me).manage_channels:
            await ctx.reply(
                embed=error_embed(
                    f"I need **Manage Channels** in {category.mention}."
                ),
                mention_author=False,
            )
            return

        data = self._data(ctx.guild)
        data["modmail_category_id"] = category.id
        data["modmail_staff_role_id"] = staff_role.id
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="📬 Modmail configured",
            description=(
                f"Modmail channels will be created in {category.mention}; "
                f"{staff_role.mention} can view and reply."
            ),
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"Modmail is ready.\n"
                f"**Category:** {category.mention}\n"
                f"**Staff role:** {staff_role.mention}\n\n"
                "Members can now DM the bot to open a private conversation.",
                title="📬 Modmail configured",
            ),
            mention_author=False,
        )

    @modmail.command(name="close", usage="[reason]")
    @commands.guild_only()
    @is_moderator("manage_messages")
    async def close(
        self, ctx: commands.Context, *, reason: str = "No reason provided"
    ) -> None:
        """Close the modmail conversation in this channel."""
        data = self._data(ctx.guild)
        user_id: int | None = None
        for raw_id, raw_channel in data.get("modmail_open", {}).items():
            try:
                if int(raw_channel) == ctx.channel.id:
                    user_id = int(raw_id)
                    break
            except (TypeError, ValueError):
                continue
        if user_id is None:
            if not self._is_modmail_channel(ctx.guild, ctx.channel):
                await ctx.reply(
                    embed=error_embed(
                        "Use this command inside an open modmail channel."
                    ),
                    mention_author=False,
                )
                return
            # The member already closed this ticket from DMs; staff only
            # need to remove the leftover channel.
            await ctx.reply(
                embed=success_embed(
                    "This ticket was already closed by the member. "
                    "Deleting the channel in 5 seconds…"
                ),
                mention_author=False,
            )
            await asyncio.sleep(5)
            try:
                await ctx.channel.delete(
                    reason=(
                        f"Modmail channel closed by {ctx.author} "
                        f"({ctx.author.id})"
                    )
                )
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ) as exc:
                log.warning(
                    "Could not delete modmail channel %s: %s",
                    ctx.channel.id,
                    exc,
                )
            return

        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                user = None

        if user is not None:
            try:
                await user.send(
                    embed=info_embed(
                        f"Your modmail with **{ctx.guild.name}** was closed.\n"
                        f"**Reason:** {reason}\n\n"
                        "You can DM me again to open a new conversation.",
                        title="📬 Modmail closed",
                    )
                )
            except discord.HTTPException:
                pass

        data.get("modmail_open", {}).pop(str(user_id), None)
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="📬 Modmail closed",
            description=(
                f"**User:** <@{user_id}> (`{user_id}`)\n"
                f"**Channel:** {ctx.channel.mention}\n"
                f"**Reason:** {truncate(reason, 500)}"
            ),
            color=SUCCESS_COLOR,
            fields=[("Closed by", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                "Modmail closed. Deleting this channel in 5 seconds…"
            ),
            mention_author=False,
        )
        await asyncio.sleep(5)
        try:
            await ctx.channel.delete(
                reason=f"Modmail closed by {ctx.author} ({ctx.author.id})"
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not delete modmail channel %s: %s", ctx.channel.id, exc)

    @modmail.command(name="reply", usage="<message>")
    @commands.guild_only()
    @is_moderator("manage_messages")
    async def reply(
        self, ctx: commands.Context, *, message: str
    ) -> None:
        """DM the user without leaving the modmail channel."""
        data = self._data(ctx.guild)
        user_id: int | None = None
        for raw_id, raw_channel in data.get("modmail_open", {}).items():
            try:
                if int(raw_channel) == ctx.channel.id:
                    user_id = int(raw_id)
                    break
            except (TypeError, ValueError):
                continue
        if user_id is None:
            await ctx.reply(
                embed=error_embed(
                    "Use this command inside an open modmail channel."
                ),
                mention_author=False,
            )
            return

        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                user = None
        if user is None:
            await ctx.reply(
                embed=error_embed("I could not find that user."),
                mention_author=False,
            )
            return

        if await self._send_to_user(user, message):
            await ctx.reply(
                embed=success_embed("Reply sent to the user."),
                mention_author=False,
            )
        else:
            await ctx.reply(
                embed=error_embed(
                    "I could not DM that user — they may have DMs closed."
                ),
                mention_author=False,
            )

    @modmail.command(name="status")
    @commands.guild_only()
    async def status(self, ctx: commands.Context) -> None:
        """Show modmail configuration and open conversations."""
        await ctx.reply(
            embed=self._status_embed(ctx.guild), mention_author=False
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Modmail(bot))
