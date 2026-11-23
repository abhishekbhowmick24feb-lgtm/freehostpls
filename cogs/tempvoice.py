"""Temporary private voice rooms created from a join-to-create channel."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from core.config import SUCCESS_COLOR, WARNING_COLOR
from core.utils import error_embed, info_embed, success_embed, truncate

log = logging.getLogger(__name__)


class TempVoice(commands.Cog):
    """Join a configured voice channel and receive your own private room."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._cleaned_up = False

    # --------------------------------------------------------------- helpers

    def _data(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)

    def _creator(self, guild: discord.Guild) -> discord.VoiceChannel | None:
        channel_id = self._data(guild).get("tempvoice_creator_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.VoiceChannel) else None

    def _category(self, guild: discord.Guild) -> discord.CategoryChannel | None:
        channel_id = self._data(guild).get("tempvoice_category_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.CategoryChannel) else None

    def _permanent_ids(self, guild: discord.Guild) -> list[int]:
        stored = self._data(guild).get("tempvoice_permanent")
        if not isinstance(stored, list):
            stored = []
            self._data(guild)["tempvoice_permanent"] = stored
        ids: list[int] = []
        for raw in stored:
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        return ids

    def _is_permanent(self, guild: discord.Guild, channel_id: int) -> bool:
        return int(channel_id) in self._permanent_ids(guild)

    def _rooms(self, guild: discord.Guild) -> dict:
        rooms = self._data(guild).get("tempvoice_rooms")
        if not isinstance(rooms, dict):
            rooms = {}
            self._data(guild)["tempvoice_rooms"] = rooms
        return rooms

    def _room_for_owner(
        self, guild: discord.Guild, owner_id: int
    ) -> discord.VoiceChannel | None:
        for raw_id, raw_owner in list(self._rooms(guild).items()):
            try:
                if int(raw_owner) != owner_id:
                    continue
                channel = guild.get_channel(int(raw_id))
            except (TypeError, ValueError):
                continue
            if isinstance(channel, discord.VoiceChannel):
                return channel
        return None

    def _owned_room(
        self, member: discord.Member
    ) -> discord.VoiceChannel | None:
        voice = getattr(member, "voice", None)
        channel = voice.channel if voice is not None else None
        if not isinstance(channel, discord.VoiceChannel):
            return None
        try:
            owner = int(self._rooms(member.guild).get(str(channel.id), 0))
        except (TypeError, ValueError):
            return None
        return channel if owner == member.id else None

    async def _owned_room_or_error(
        self, ctx: commands.Context
    ) -> discord.VoiceChannel | None:
        room = self._owned_room(ctx.author)
        if room is None:
            await ctx.reply(
                embed=error_embed(
                    "Join a temporary room **you own** first. Only the room's "
                    "owner can use this."
                ),
                mention_author=False,
            )
            return None
        me = ctx.guild.me
        if me is None or not room.permissions_for(me).manage_channels:
            await ctx.reply(
                embed=error_embed(
                    f"I need **Manage Channels** in {room.mention} to do that."
                ),
                mention_author=False,
            )
            return None
        return room

    async def _create_room(self, member: discord.Member) -> None:
        guild = member.guild
        creator = self._creator(guild)
        if creator is None:
            return
        me = guild.me
        if me is None:
            log.warning("Cannot create a temp voice room: bot member unavailable")
            return
        if not creator.permissions_for(me).move_members:
            log.warning(
                "Cannot create a temp voice room: missing Move Members in %s",
                creator.id,
            )
            return

        existing = self._room_for_owner(guild, member.id)
        if existing is not None:
            try:
                await member.move_to(existing, reason="Temporary voice room")
                return
            except (discord.Forbidden, discord.HTTPException):
                pass

        category = self._category(guild) or creator.category
        if category is not None:
            category_permissions = category.permissions_for(me)
            if not category_permissions.manage_channels:
                log.warning(
                    "Cannot create a temp voice room in category %s: missing Manage Channels",
                    category.id,
                )
                return
        elif not me.guild_permissions.manage_channels:
            log.warning("Cannot create a temp voice room: missing Manage Channels")
            return

        name = truncate(f"{member.display_name}'s room", 100)
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False, connect=False
            ),
            member: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                stream=True,
                use_voice_activation=True,
                manage_channels=True,
                move_members=True,
            ),
            me: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                send_messages=True,
                embed_links=True,
                read_message_history=True,
                manage_channels=True,
                move_members=True,
            ),
        }

        try:
            channel = await guild.create_voice_channel(
                name,
                category=category,
                overwrites=overwrites,
                reason=f"Temporary voice room for {member} ({member.id})",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not create temp voice room for %s: %s", member.id, exc)
            return

        self._rooms(guild)[str(channel.id)] = member.id
        await self.bot.db.save()
        try:
            await member.move_to(channel, reason="Moved into your temporary voice room")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not move %s into temp room: %s", member.id, exc)
        await self._send_panel(channel)

    async def _send_panel(self, channel: discord.VoiceChannel) -> None:
        """Post the owner control panel in the room's text chat."""
        try:
            await channel.send(
                embed=room_panel_embed(channel), view=TempRoomView(channel)
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning(
                "Could not post the temp room panel in %s: %s", channel.id, exc
            )

    async def _cleanup_room(self, channel: discord.VoiceChannel) -> None:
        await asyncio.sleep(2)
        guild = channel.guild
        rooms = self._rooms(guild)
        if str(channel.id) not in rooms:
            return
        fresh = guild.get_channel(channel.id)
        if not isinstance(fresh, discord.VoiceChannel):
            rooms.pop(str(channel.id), None)
            await self.bot.db.save()
            return
        if fresh.members:
            return
        if self._is_permanent(guild, channel.id):
            return
        try:
            await fresh.delete(reason="Temporary voice room is empty")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        rooms.pop(str(channel.id), None)
        await self.bot.db.save()

    # --------------------------------------------------------------- events

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot or member.guild is None:
            return
        creator = self._creator(member.guild)
        if creator is not None and after.channel is not None:
            if after.channel.id == creator.id:
                await self._create_room(member)

        if before.channel is not None:
            if str(before.channel.id) in self._rooms(member.guild):
                await self._cleanup_room(before.channel)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if not isinstance(channel, discord.VoiceChannel):
            return
        changed = False
        rooms = self._rooms(channel.guild)
        if str(channel.id) in rooms:
            rooms.pop(str(channel.id), None)
            changed = True
        permanent = self._permanent_ids(channel.guild)
        if channel.id in permanent:
            permanent.remove(channel.id)
            changed = True
        if changed:
            await self.bot.db.save()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # on_ready fires again after every reconnect. Sweep only once so rooms
        # are not deleted while voice states are still being filled in.
        if self._cleaned_up:
            return
        self._cleaned_up = True

        changed = False
        for guild in self.bot.guilds:
            rooms = self._rooms(guild)
            for raw_id in list(rooms):
                try:
                    channel = guild.get_channel(int(raw_id))
                except (TypeError, ValueError):
                    rooms.pop(raw_id, None)
                    changed = True
                    continue
                if not isinstance(channel, discord.VoiceChannel):
                    rooms.pop(raw_id, None)
                    changed = True
                    continue
                if channel.members or self._is_permanent(guild, channel.id):
                    continue
                try:
                    await channel.delete(
                        reason="Temporary voice room empty (startup cleanup)"
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
                rooms.pop(raw_id, None)
                changed = True
        if changed:
            await self.bot.db.save()

    # -------------------------------------------------------------- commands

    def _status_embed(self, guild: discord.Guild) -> discord.Embed:
        creator = self._creator(guild)
        category = self._category(guild)
        rooms = self._rooms(guild)
        active = []
        for raw_id in rooms:
            try:
                channel = guild.get_channel(int(raw_id))
            except (TypeError, ValueError):
                continue
            if isinstance(channel, discord.VoiceChannel):
                active.append(channel)
        permanent = self._permanent_ids(guild)
        listing = "\n".join(
            f"• {channel.mention}"
            + (" 📌 permanent" if channel.id in permanent else "")
            for channel in active[:10]
        )
        if len(active) > 10:
            listing += f"\n… and {len(active) - 10} more."

        if creator is not None and guild.me is not None:
            creator_permissions = creator.permissions_for(guild.me)
            missing = []
            if not creator_permissions.view_channel:
                missing.append("View Channel")
            if not creator_permissions.move_members:
                missing.append("Move Members")
            if category is not None:
                if not category.permissions_for(guild.me).manage_channels:
                    missing.append("Manage Channels")
            elif not guild.me.guild_permissions.manage_channels:
                missing.append("Manage Channels")
            permissions_line = (
                "**Permissions:** ready"
                if not missing
                else f"**Permissions:** missing {', '.join(missing)}"
            )
        else:
            permissions_line = "**Permissions:** set a creator channel first"

        return info_embed(
            f"**Creator channel:** {creator.mention if creator else 'not set'}\n"
            f"**Room category:** {category.mention if category else 'creator category'}\n"
            f"{permissions_line}\n"
            f"**Active rooms:** `{len(active)}` • **Permanent:** "
            f"`{sum(1 for channel in active if channel.id in permanent)}`\n"
            + (f"\n{listing}" if listing else "")
            + "\n\n**Owner controls:** a panel is posted in each new room "
            "(private, public, member limit), and `/tempvoice lock`, "
            "`unlock`, `rename`, `transfer` work for the owner\n"
            "**Setup:** `/tempvoice setup creator:#Create-VC "
            "[category:#Voice]`",
            title="🔊 Temporary voice rooms",
        )

    @commands.hybrid_group(
        name="tempvoice",
        aliases=["tempvc", "voicehub"],
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def tempvoice(self, ctx: commands.Context) -> None:
        """Set up join-to-create private voice rooms."""
        await ctx.reply(
            embed=self._status_embed(ctx.guild), mention_author=False
        )

    @tempvoice.command(name="setup", usage="<#create-vc> [category]")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        creator="Voice channel members join to create their room",
        category="Where new rooms are created; defaults to the creator's category",
    )
    async def setup(
        self,
        ctx: commands.Context,
        creator: discord.VoiceChannel,
        category: discord.CategoryChannel | None = None,
    ) -> None:
        """Choose the join-to-create channel and optional room category."""
        me = ctx.guild.me
        if me is None:
            await ctx.reply(
                embed=error_embed("I could not read my server permissions."),
                mention_author=False,
            )
            return

        category = category or creator.category
        creator_permissions = creator.permissions_for(me)
        missing = []
        if not creator_permissions.view_channel:
            missing.append("View Channel")
        if not creator_permissions.move_members:
            missing.append("Move Members")
        if missing:
            await ctx.reply(
                embed=error_embed(
                    f"I need **{'**, **'.join(missing)}** in {creator.mention}.\n"
                    "Server-level permissions are not enough if the channel or "
                    "its category overwrites them — check the channel's "
                    "permission settings."
                ),
                mention_author=False,
            )
            return

        if category is not None:
            if not category.permissions_for(me).manage_channels:
                await ctx.reply(
                    embed=error_embed(
                        f"I need **Manage Channels** in {category.mention}.\n"
                        "Check the category's permission overwrites."
                    ),
                    mention_author=False,
                )
                return
        elif not me.guild_permissions.manage_channels:
            await ctx.reply(
                embed=error_embed(
                    "I need **Manage Channels** at the server level because "
                    "there is no room category selected."
                ),
                mention_author=False,
            )
            return
        data = self._data(ctx.guild)
        data["tempvoice_creator_id"] = creator.id
        data["tempvoice_category_id"] = category.id if category else None
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🔊 Temporary voice rooms configured",
            description=(
                f"Members who join {creator.mention} get a private room"
                + (f" in {category.mention}." if category else ".")
            ),
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"Join-to-create is ready.\n"
                f"**Creator channel:** {creator.mention}\n"
                f"**Room category:** {category.mention if category else 'creator category'}\n\n"
                "A member who joins the creator gets a private room they can "
                "manage. Empty rooms are deleted automatically.",
                title="🔊 Temporary voice rooms ready",
            ),
            mention_author=False,
        )

    @tempvoice.command(name="disable", aliases=["off", "reset"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def disable(self, ctx: commands.Context) -> None:
        """Stop creating new temporary voice rooms."""
        data = self._data(ctx.guild)
        data["tempvoice_creator_id"] = None
        data["tempvoice_category_id"] = None
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🔊 Temporary voice rooms disabled",
            description="New rooms will no longer be created. Existing rooms are left alone.",
            color=WARNING_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                "Join-to-create is **off**. Existing temporary rooms are left alone."
            ),
            mention_author=False,
        )

    @tempvoice.command(name="lock")
    @commands.guild_only()
    async def lock(self, ctx: commands.Context) -> None:
        """Stop other members from seeing or joining your room."""
        room = await self._owned_room_or_error(ctx)
        if room is None:
            return
        try:
            await room.set_permissions(
                ctx.guild.default_role,
                view_channel=False,
                connect=False,
                reason=f"Room locked by owner {ctx.author} ({ctx.author.id})",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.reply(
                embed=error_embed(f"I could not lock the room: {exc}"),
                mention_author=False,
            )
            return
        await ctx.reply(
            embed=success_embed(
                f"{room.mention} is private — only you can see and join it.\n"
                "Use `/tempvoice unlock` to let others in.",
                title="🔒 Room locked",
            ),
            mention_author=False,
        )

    @tempvoice.command(name="unlock")
    @commands.guild_only()
    async def unlock(self, ctx: commands.Context) -> None:
        """Let other members see and join your room."""
        room = await self._owned_room_or_error(ctx)
        if room is None:
            return
        try:
            await room.set_permissions(
                ctx.guild.default_role,
                view_channel=True,
                connect=True,
                reason=f"Room unlocked by owner {ctx.author} ({ctx.author.id})",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.reply(
                embed=error_embed(f"I could not unlock the room: {exc}"),
                mention_author=False,
            )
            return
        await ctx.reply(
            embed=success_embed(
                f"{room.mention} is open — other members can see and join it.",
                title="🔓 Room unlocked",
            ),
            mention_author=False,
        )

    @tempvoice.command(name="rename", usage="<name>")
    @commands.guild_only()
    @discord.app_commands.describe(name="New name for your room")
    async def rename(self, ctx: commands.Context, *, name: str) -> None:
        """Rename your temporary room."""
        room = await self._owned_room_or_error(ctx)
        if room is None:
            return
        cleaned = truncate(" ".join(name.split()), 100)
        if not cleaned:
            await ctx.reply(
                embed=error_embed("Give the room a name."),
                mention_author=False,
            )
            return
        try:
            await room.edit(
                name=cleaned,
                reason=f"Renamed by room owner {ctx.author} ({ctx.author.id})",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.reply(
                embed=error_embed(f"I could not rename the room: {exc}"),
                mention_author=False,
            )
            return
        await ctx.reply(
            embed=success_embed(
                f"Your room is now {room.mention}.", title="✏️ Room renamed"
            ),
            mention_author=False,
        )

    @tempvoice.command(name="transfer", usage="<member>")
    @commands.guild_only()
    @discord.app_commands.describe(
        member="Member in your room who becomes the new owner"
    )
    async def transfer(
        self, ctx: commands.Context, member: discord.Member
    ) -> None:
        """Hand your temporary room to another member in it."""
        room = await self._owned_room_or_error(ctx)
        if room is None:
            return
        if member.bot:
            await ctx.reply(
                embed=error_embed("A bot cannot own a room."),
                mention_author=False,
            )
            return
        if member.id == ctx.author.id:
            await ctx.reply(
                embed=error_embed("You already own this room."),
                mention_author=False,
            )
            return
        if member not in room.members:
            await ctx.reply(
                embed=error_embed(
                    f"{member.mention} must be in {room.mention} to take it over."
                ),
                mention_author=False,
            )
            return
        reason = (
            f"Room transferred from {ctx.author} ({ctx.author.id}) "
            f"to {member} ({member.id})"
        )
        try:
            await room.set_permissions(
                ctx.author,
                view_channel=True,
                connect=True,
                manage_channels=False,
                move_members=False,
                reason=reason,
            )
            await room.set_permissions(
                member,
                view_channel=True,
                connect=True,
                speak=True,
                stream=True,
                use_voice_activation=True,
                manage_channels=True,
                move_members=True,
                reason=reason,
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.reply(
                embed=error_embed(f"I could not transfer the room: {exc}"),
                mention_author=False,
            )
            return
        self._rooms(ctx.guild)[str(room.id)] = member.id
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(
                f"{member.mention} now owns {room.mention}. "
                "The room's controls work for them from now on.",
                title="👑 Room transferred",
            ),
            mention_author=False,
        )

    @tempvoice.command(name="permanent", usage="<#room> [permanent]")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @discord.app_commands.describe(
        channel="A temporary room created from the join-to-create channel",
        permanent="True keeps it when empty; false makes it temporary again",
    )
    async def permanent(
        self,
        ctx: commands.Context,
        channel: discord.VoiceChannel,
        permanent: bool = True,
    ) -> None:
        """Keep a temporary room after everyone leaves (staff only)."""
        if str(channel.id) not in self._rooms(ctx.guild):
            await ctx.reply(
                embed=error_embed(
                    "That is not a temporary room created from the "
                    "join-to-create channel."
                ),
                mention_author=False,
            )
            return
        ids = self._permanent_ids(ctx.guild)
        already = channel.id in ids
        if permanent and not already:
            ids.append(channel.id)
        elif not permanent and already:
            ids.remove(channel.id)
        else:
            await ctx.reply(
                embed=info_embed(
                    f"{channel.mention} is already "
                    + ("permanent." if permanent else "temporary.")
                ),
                mention_author=False,
            )
            return
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title=(
                "📌 Temporary room made permanent"
                if permanent
                else "📌 Temporary room made temporary"
            ),
            description=(
                f"{channel.mention} will "
                + ("stay when empty." if permanent else "be deleted when empty again.")
            ),
            color=SUCCESS_COLOR if permanent else WARNING_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"{channel.mention} is now **permanent**. It stays when empty "
                "and is not removed at startup."
                if permanent
                else f"{channel.mention} is **temporary** again and will be "
                "deleted when everyone leaves.",
                title="📌 Permanent" if permanent else "📌 Temporary",
            ),
            mention_author=False,
        )

    @tempvoice.command(name="status")
    @commands.guild_only()
    async def status(self, ctx: commands.Context) -> None:
        """Show the join-to-create settings and active rooms."""
        await ctx.reply(
            embed=self._status_embed(ctx.guild), mention_author=False
        )


# ------------------------------------------------------------------- panel


def _is_private(channel: discord.VoiceChannel) -> bool:
    overwrite = channel.overwrites_for(channel.guild.default_role)
    return overwrite.view_channel is False or overwrite.connect is False


def room_panel_embed(channel: discord.VoiceChannel) -> discord.Embed:
    """The owner panel shown inside a temporary room."""
    limit = channel.user_limit or 0
    embed = info_embed(
        "These buttons control **your** room. Only the room's owner can use "
        "them.",
        title="🔊 Your temporary room",
    )
    embed.add_field(name="Room", value=channel.mention, inline=True)
    embed.add_field(
        name="Visibility",
        value="🔒 Private" if _is_private(channel) else "🌐 Public",
        inline=True,
    )
    embed.add_field(
        name="Member limit",
        value=f"`{limit}`" if limit else "No limit",
        inline=True,
    )
    embed.set_footer(text="Empty rooms are deleted automatically.")
    return embed


class TempLimitModal(discord.ui.Modal, title="Member limit"):
    """Asks the owner how many members the room may hold."""

    limit = discord.ui.TextInput(
        label="Maximum members",
        placeholder="A number from 1 to 99",
        max_length=2,
    )

    def __init__(self, panel_id: int | None = None) -> None:
        super().__init__(timeout=300)
        self.panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        room = await _owner_room(interaction)
        if room is None:
            return
        raw = str(self.limit.value).strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 99:
            await interaction.response.send_message(
                embed=error_embed("Give a whole number from 1 to 99."),
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        try:
            await room.edit(
                user_limit=int(raw),
                reason=(
                    f"Member limit set by room owner "
                    f"{interaction.user} ({interaction.user.id})"
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await _follow(interaction, error_embed(f"I could not set the limit: {exc}"))
            return
        await _refresh_panel(interaction, room, self.panel_id)
        await _follow(
            interaction,
            success_embed(
                f"{room.mention} now holds up to **{int(raw)}** members."
            ),
        )


class TempRoomView(discord.ui.View):
    """Persistent owner controls posted in every temporary room."""

    def __init__(self, channel: discord.VoiceChannel | None = None) -> None:
        super().__init__(timeout=None)
        private = channel is not None and _is_private(channel)
        limit = channel.user_limit if channel is not None else 0

        lock = discord.ui.Button(
            label="Private",
            emoji="🔒",
            style=discord.ButtonStyle.secondary,
            custom_id="tempvoice:private",
            disabled=private,
        )
        unlock = discord.ui.Button(
            label="Public",
            emoji="🌐",
            style=discord.ButtonStyle.success,
            custom_id="tempvoice:public",
            disabled=channel is not None and not private,
        )
        set_limit = discord.ui.Button(
            label="Set limit",
            emoji="👥",
            style=discord.ButtonStyle.primary,
            custom_id="tempvoice:limit",
        )
        no_limit = discord.ui.Button(
            label="No limit",
            emoji="♾️",
            style=discord.ButtonStyle.secondary,
            custom_id="tempvoice:nolimit",
            disabled=channel is not None and limit == 0,
        )
        lock.callback = self._private
        unlock.callback = self._public
        set_limit.callback = self._set_limit
        no_limit.callback = self._clear_limit
        for button in (lock, unlock, set_limit, no_limit):
            self.add_item(button)

    async def _private(self, interaction: discord.Interaction) -> None:
        room = await _owner_room(interaction)
        if room is None:
            return
        await interaction.response.defer()
        try:
            await room.set_permissions(
                room.guild.default_role,
                view_channel=False,
                connect=False,
                reason=(
                    f"Room made private by owner "
                    f"{interaction.user} ({interaction.user.id})"
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await _follow(interaction, error_embed(f"I could not change the room: {exc}"))
            return
        await _refresh_panel(interaction, room)
        await _follow(
            interaction,
            success_embed(
                f"{room.mention} is **private** — only you can see and join it."
            ),
        )

    async def _public(self, interaction: discord.Interaction) -> None:
        room = await _owner_room(interaction)
        if room is None:
            return
        await interaction.response.defer()
        try:
            await room.set_permissions(
                room.guild.default_role,
                view_channel=True,
                connect=True,
                reason=(
                    f"Room made public by owner "
                    f"{interaction.user} ({interaction.user.id})"
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await _follow(interaction, error_embed(f"I could not change the room: {exc}"))
            return
        await _refresh_panel(interaction, room)
        await _follow(
            interaction,
            success_embed(
                f"{room.mention} is **public** — anyone with access can join."
            ),
        )

    async def _set_limit(self, interaction: discord.Interaction) -> None:
        room = await _owner_room(interaction, need_manage=False)
        if room is None:
            return
        panel_id = interaction.message.id if interaction.message else None
        await interaction.response.send_modal(TempLimitModal(panel_id))

    async def _clear_limit(self, interaction: discord.Interaction) -> None:
        room = await _owner_room(interaction)
        if room is None:
            return
        await interaction.response.defer()
        try:
            await room.edit(
                user_limit=0,
                reason=(
                    f"Member limit removed by owner "
                    f"{interaction.user} ({interaction.user.id})"
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await _follow(interaction, error_embed(f"I could not remove the limit: {exc}"))
            return
        await _refresh_panel(interaction, room)
        await _follow(
            interaction,
            success_embed(f"{room.mention} has **no member limit** now."),
        )


async def _owner_room(
    interaction: discord.Interaction, *, need_manage: bool = True
) -> discord.VoiceChannel | None:
    """The room behind a panel button, when the clicker owns it."""
    cog = interaction.client.get_cog("TempVoice")
    guild = interaction.guild
    channel = interaction.channel
    if cog is None or guild is None or not isinstance(channel, discord.VoiceChannel):
        await interaction.response.send_message(
            embed=error_embed("This panel only works inside a temporary room."),
            ephemeral=True,
        )
        return None
    owner = cog._rooms(guild).get(str(channel.id))
    if owner is None:
        await interaction.response.send_message(
            embed=error_embed("This is no longer a temporary room."),
            ephemeral=True,
        )
        return None
    try:
        is_owner = int(owner) == interaction.user.id
    except (TypeError, ValueError):
        is_owner = False
    if not is_owner:
        await interaction.response.send_message(
            embed=error_embed("Only the room's owner can change it."),
            ephemeral=True,
        )
        return None
    me = guild.me
    if need_manage and (me is None or not channel.permissions_for(me).manage_channels):
        await interaction.response.send_message(
            embed=error_embed(
                f"I need **Manage Channels** in {channel.mention} to do that."
            ),
            ephemeral=True,
        )
        return None
    return channel


async def _refresh_panel(
    interaction: discord.Interaction,
    room: discord.VoiceChannel,
    panel_id: int | None = None,
) -> None:
    """Redraw the panel message so its buttons and fields match the room."""
    message = interaction.message
    if message is None and panel_id is not None:
        try:
            message = await room.fetch_message(panel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None
    if message is None:
        return
    try:
        await message.edit(
            embed=room_panel_embed(room), view=TempRoomView(room)
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


async def _follow(
    interaction: discord.Interaction, embed: discord.Embed
) -> None:
    """Send the private confirmation for a panel action."""
    try:
        await interaction.followup.send(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TempVoice(bot))
