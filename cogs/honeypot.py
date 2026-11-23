"""Honeypot channel: anyone who talks there is softbanned automatically."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.config import ERROR_COLOR, SUCCESS_COLOR, WARNING_COLOR
from core.utils import (
    ConfirmView,
    error_embed,
    info_embed,
    is_whitelisted,
    success_embed,
)

log = logging.getLogger(__name__)

PANEL_COLOR = 0x2B2D31
PANEL_TITLE = "Do not send messages in this channel"
PANEL_DESCRIPTION = (
    "This channel is a trap for compromised accounts and for anyone who clicks "
    "scam links.\n\n"
    "Anything sent here is deleted and the sender is removed with a **softban** "
    "(banned and immediately unbanned).\n\n"
    "Staff are exempt. If you were removed by mistake, ask a staff member for an invite."
)
SOFTBAN_REASON = "Honeypot: sent a message in a no-talk channel"


def panel_embed(guild: discord.Guild, count: int) -> discord.Embed:
    """The honeypot notice. The bot's own avatar replaces the shield picture."""
    embed = discord.Embed(
        title=PANEL_TITLE,
        description=PANEL_DESCRIPTION,
        color=PANEL_COLOR,
    )
    avatar = guild.me.display_avatar if guild.me else None
    if avatar is not None:
        embed.set_thumbnail(url=avatar.url)
    return embed


def panel_view(count: int) -> discord.ui.View:
    """A disabled button that displays the kick counter, like a stat chip."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label=f"Kicks: {count}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
    )
    return view


def removal_embed(
    guild: discord.Guild, channel_name: str, invite_url: str | None
) -> discord.Embed:
    """The DM a honeypot victim receives, with safety steps and a way back in."""
    embed = discord.Embed(
        title=f"You were removed from {guild.name}",
        description=(
            f"Your account posted in **#{channel_name}**, a channel nobody is meant "
            "to use. That usually means someone else has got into your account."
        ),
        color=ERROR_COLOR,
    )
    embed.add_field(
        name="Make your account safe",
        value=(
            "1. Change your password. That logs the other person out.\n"
            "2. Turn on two-factor authentication.\n"
            "3. In Settings, Authorized Apps, remove any you don't know.\n"
            "4. Run a virus scan. Scam links often install a token stealer."
        ),
        inline=False,
    )
    embed.add_field(
        name="Discord's guide to a hacked account",
        value=(
            "[Open Discord's help article]"
            "(https://support.discord.com/hc/en-us/articles/24160905919511)"
        ),
        inline=False,
    )
    if invite_url:
        embed.add_field(
            name="Rejoin the server",
            value=(
                "Once your account is safe, use this invite to come back: "
                f"{invite_url}"
            ),
            inline=False,
        )
        embed.set_footer(
            text="Use the invite above to rejoin once your account is safe."
        )
    else:
        embed.set_footer(
            text="Ask a staff member for an invite once your account is safe."
        )
    return embed


class Honeypot(commands.Cog):
    """Catch compromised accounts with a channel nobody should talk in."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # --------------------------------------------------------------- helpers

    def _config(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)["honeypot"]

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
        config = self._config(guild)
        channel = guild.get_channel(config.get("channel_id") or 0)
        if not isinstance(channel, discord.TextChannel) or not config.get("message_id"):
            return False
        try:
            message = await channel.fetch_message(config["message_id"])
            await message.edit(
                embed=panel_embed(guild, config["count"]),
                view=panel_view(config["count"]),
            )
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    @staticmethod
    def _is_staff(member: discord.Member, guild: discord.Guild, data: dict) -> bool:
        """Staff, moderators and whitelisted people are never punished."""
        if is_whitelisted(member, data):
            return True
        if member.id == guild.owner_id or member.guild_permissions.administrator:
            return True
        if member.guild_permissions.manage_messages:
            return True
        role_id = data.get("mod_role_id")
        if role_id:
            role = guild.get_role(role_id)
            if role is not None and role in member.roles:
                return True
        return False

    async def _create_rejoin_invite(self, guild: discord.Guild) -> str | None:
        """Return the saved rejoin invite, or make a one-use fallback."""
        config = self._config(guild)
        saved = config.get("rejoin_invite")
        if saved:
            return saved

        me = guild.me
        if me is None:
            return None

        candidates: list[discord.TextChannel] = []
        if guild.system_channel is not None:
            candidates.append(guild.system_channel)
        candidates.extend(guild.text_channels)

        seen: set[int] = set()
        for channel in candidates:
            if channel.id in seen:
                continue
            seen.add(channel.id)
            permissions = channel.permissions_for(me)
            if not (permissions.view_channel and permissions.create_instant_invite):
                continue
            try:
                invite = await channel.create_invite(
                    max_uses=1,
                    max_age=0,
                    unique=True,
                    reason="Honeypot softban — one-use rejoin invite",
                )
                return invite.url
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.debug(
                    "Could not create a honeypot invite in channel %s: %s",
                    channel.id,
                    exc,
                )

        try:
            if guild.vanity_url:
                return guild.vanity_url
        except AttributeError:
            pass
        return None

    async def _send_removal_dm(
        self, member: discord.Member, guild: discord.Guild, channel_name: str
    ) -> bool:
        """DM the victim before they are banned; after a ban the DM cannot be sent."""
        invite_url = await self._create_rejoin_invite(guild)
        view = None
        if invite_url:
            view = discord.ui.View(timeout=None)
            view.add_item(
                discord.ui.Button(
                    label="Rejoin server",
                    style=discord.ButtonStyle.link,
                    url=invite_url,
                )
            )
        try:
            await member.send(
                embed=removal_embed(guild, channel_name, invite_url), view=view
            )
            return True
        except discord.Forbidden:
            log.info(
                "Honeypot could not DM %s (DMs closed or no mutual guild)", member.id
            )
        except discord.HTTPException as exc:
            log.warning("Honeypot could not DM %s: %s", member.id, exc)
        return False

    # ---------------------------------------------------------------- group

    @commands.hybrid_group(
        name="honeypot",
        aliases=["hp"],
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def honeypot(self, ctx: commands.Context) -> None:
        """A trap channel that softbans anyone who sends a message in it."""
        config = self._config(ctx.guild)
        channel = ctx.guild.get_channel(config.get("channel_id") or 0)
        embed = info_embed(
            f"**Channel:** {channel.mention if channel else 'not set'}\n"
            f"**Kicks:** `{config['count']}`\n"
            f"**Panel:** {'posted' if config.get('message_id') else 'not posted'}\n"
            f"**Rejoin invite:** "
            f"{config.get('rejoin_invite') or 'not set (one-use invite per softban)'}\n\n"
            f"**Setup:** `{ctx.prefix}honeypot setup #channel`\n"
            f"Anyone who types there is softbanned (banned and instantly unbanned).\n"
            "Staff, moderators and administrators are never punished.",
            title="🍯 Honeypot",
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ---------------------------------------------------------------- setup

    @honeypot.command(name="setup", usage="<#channel>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_messages=True, ban_members=True)
    async def setup(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Post the honeypot notice in a channel and arm it."""
        permissions = channel.permissions_for(ctx.guild.me)
        missing = [
            name
            for name, ok in (
                ("View Channel", permissions.view_channel),
                ("Send Messages", permissions.send_messages),
                ("Embed Links", permissions.embed_links),
                ("Manage Messages", permissions.manage_messages),
            )
            if not ok
        ]
        if missing:
            await ctx.reply(
                embed=error_embed(
                    f"I need **{', '.join(missing)}** in {channel.mention}."
                ),
                mention_author=False,
            )
            return

        config = self._config(ctx.guild)
        await self._delete_panel(ctx.guild, config)

        config["channel_id"] = channel.id
        config["message_id"] = None
        try:
            message = await channel.send(
                embed=panel_embed(ctx.guild, config["count"]),
                view=panel_view(config["count"]),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            await ctx.reply(
                embed=error_embed(f"I could not post the panel: {exc}"),
                mention_author=False,
            )
            return

        config["message_id"] = message.id
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🍯 Honeypot armed",
            description=f"Honeypot panel posted in {channel.mention}.",
            color=WARNING_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"Honeypot armed in {channel.mention}.\n"
                "Anyone who sends a message there is **softbanned**: their message "
                "is deleted, they are banned and immediately unbanned.\n"
                "Staff, your moderator role and administrators are exempt.",
                title="🍯 Honeypot ready",
            ),
            mention_author=False,
        )

    # --------------------------------------------------------------- control

    @honeypot.command(name="disable", aliases=["off"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def disable(self, ctx: commands.Context) -> None:
        """Disarm the honeypot and remove its panel."""
        config = self._config(ctx.guild)
        if not config.get("channel_id"):
            await ctx.reply(
                embed=info_embed("The honeypot is not set up."), mention_author=False
            )
            return
        if not await ConfirmView.prompt(
            ctx, "Disable the honeypot and delete its panel? The kick counter is kept."
        ):
            return
        await self._delete_panel(ctx.guild, config)
        config["channel_id"] = None
        config["message_id"] = None
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🍯 Honeypot disabled",
            description="The honeypot channel is no longer armed.",
            color=ERROR_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed("Honeypot disabled and its panel removed."),
            mention_author=False,
        )

    @honeypot.command(name="reset", aliases=["resetcount"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def reset(self, ctx: commands.Context) -> None:
        """Reset the kick counter back to zero."""
        config = self._config(ctx.guild)
        config["count"] = 0
        await self.bot.db.save()
        await self._refresh_panel(ctx.guild)
        await ctx.reply(
            embed=success_embed("Honeypot counter reset to `0`."),
            mention_author=False,
        )

    @honeypot.command(name="status")
    @commands.guild_only()
    async def status(self, ctx: commands.Context) -> None:
        """Show the current honeypot settings."""
        await self.honeypot(ctx)

    @honeypot.command(
        name="invite", aliases=["rejoin", "link"], usage="<url|none>"
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def invite(
        self, ctx: commands.Context, *, value: str | None = None
    ) -> None:
        """Set one permanent invite sent to honeypot victims."""
        config = self._config(ctx.guild)

        if not value:
            current = config.get("rejoin_invite")
            await ctx.reply(
                embed=info_embed(
                    f"**Current rejoin invite:** {current or 'not set'}\n"
                    f"Set one with `{ctx.prefix}honeypot invite <invite link>`,\n"
                    f"or clear it with `{ctx.prefix}honeypot invite none`.\n\n"
                    "Until one is set, the bot creates a one-use invite for each "
                    "honeypot softban.",
                    title="🍯 Honeypot rejoin invite",
                ),
                mention_author=False,
            )
            return

        if value.strip().lower() in {"none", "clear", "off", "reset", "disable"}:
            config["rejoin_invite"] = None
            await self.bot.db.save()
            await self.bot.audit(
                ctx.guild,
                title="🍯 Rejoin invite cleared",
                description="The honeypot rejoin invite was cleared.",
                color=WARNING_COLOR,
                fields=[("By", ctx.author.mention, True)],
            )
            await ctx.reply(
                embed=success_embed(
                    "The honeypot rejoin invite was cleared.\n"
                    "Until a new one is set, the bot creates a one-use invite per softban."
                ),
                mention_author=False,
            )
            return

        try:
            invite = await self.bot.fetch_invite(value.strip())
        except discord.NotFound:
            await ctx.reply(
                embed=error_embed("That invite is invalid or has expired."),
                mention_author=False,
            )
            return
        except discord.HTTPException as exc:
            await ctx.reply(
                embed=error_embed(f"Discord rejected that invite: {exc}"),
                mention_author=False,
            )
            return

        if invite.guild is None or invite.guild.id != ctx.guild.id:
            await ctx.reply(
                embed=error_embed("That invite belongs to a different server."),
                mention_author=False,
            )
            return

        config["rejoin_invite"] = invite.url
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🍯 Rejoin invite set",
            description=(
                f"Honeypot victims will be sent {invite.url} after a softban."
            ),
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )

        note = ""
        if invite.max_age or invite.max_uses:
            limits = []
            if invite.max_age:
                limits.append(f"expires in {invite.max_age}s")
            if invite.max_uses:
                limits.append(f"{invite.max_uses} uses max")
            note = (
                "\n⚠️ This invite is not permanent ("
                + ", ".join(limits)
                + ")."
            )

        await ctx.reply(
            embed=success_embed(
                f"**Rejoin invite:** {invite.url}\n"
                "Honeypot victims will get this same invite in their DM."
                + note,
                title="🍯 Rejoin invite saved",
            ),
            mention_author=False,
        )

    # -------------------------------------------------------------- catching

    async def _ensure_panel(self, guild: discord.Guild) -> None:
        """Keep the panel alive across restarts and redeploys."""
        config = self._config(guild)
        channel = guild.get_channel(config.get("channel_id") or 0)
        if not isinstance(channel, discord.TextChannel):
            return
        if config.get("message_id") and await self._refresh_panel(guild):
            return
        try:
            message = await channel.send(
                embed=panel_embed(guild, config["count"]),
                view=panel_view(config["count"]),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.info(
                "Honeypot could not re-post its panel in guild %s: %s", guild.id, exc
            )
            return
        config["message_id"] = message.id
        await self.bot.db.save()
        log.info("Honeypot panel restored in guild %s", guild.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Re-post the panel if it was deleted or its saved id went stale."""
        for guild in self.bot.guilds:
            try:
                await self._ensure_panel(guild)
            except Exception:
                log.exception("Honeypot panel restore failed in guild %s", guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        config = self._config(message.guild)
        if not config.get("channel_id") or message.channel.id != config["channel_id"]:
            return

        member = message.author
        data = self.bot.db.guild(message.guild.id)
        if self._is_staff(member, message.guild, data):
            return

        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        me = message.guild.me
        can_punish = (
            me is not None
            and me.guild_permissions.ban_members
            and me.top_role > member.top_role
        )
        # delete_message_seconds needs Manage Messages; without it Discord
        # rejects the whole ban, so only ask for message cleanup when allowed.
        ban_kwargs: dict = {"reason": SOFTBAN_REASON}
        if me is not None and me.guild_permissions.manage_messages:
            ban_kwargs["delete_message_seconds"] = 86400

        punished = False
        dm_sent = False
        if can_punish:
            # Send the DM first: once the member is banned the bot no longer
            # shares a guild with them, and Discord refuses bot DMs without one.
            dm_sent = await self._send_removal_dm(
                member, message.guild, message.channel.name
            )
            try:
                await message.guild.ban(member, **ban_kwargs)
                try:
                    await message.guild.unban(
                        member, reason=f"Softban reversed (honeypot) — {member.id}"
                    )
                    punished = True
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.error(
                        "Honeypot banned %s but could not unban them: %s",
                        member.id,
                        exc,
                    )
            except discord.Forbidden:
                log.warning(
                    "Honeypot could not ban %s in guild %s (missing permission or role too low)",
                    member.id,
                    message.guild.id,
                )
            except discord.HTTPException as exc:
                log.warning("Honeypot softban failed for %s: %s", member.id, exc)
        else:
            log.warning(
                "Honeypot skipped %s in guild %s (bot cannot ban: permission or role hierarchy)",
                member.id,
                message.guild.id,
            )

        if punished:
            config["count"] += 1
            await self.bot.db.save()
            await self._refresh_panel(message.guild)

        await self.bot.audit(
            message.guild,
            title="🍯 Honeypot triggered",
            description=f"{member.mention} sent a message in {message.channel.mention}.",
            color=ERROR_COLOR,
            fields=[
                ("User", f"{member} (`{member.id}`)", True),
                ("Result", "Softbanned" if punished else "Not punished", True),
                ("DM", "Sent" if dm_sent else "Not sent", True),
                ("Kicks", str(config["count"]), True),
            ],
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Honeypot(bot))
