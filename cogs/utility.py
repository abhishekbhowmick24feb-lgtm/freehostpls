"""Core utility commands: help, prefix, announcements and information."""

from __future__ import annotations

import platform
import time

import discord
from discord.ext import commands

from cogs.moderation import is_moderator
from core.config import DEFAULT_PREFIX
from core.utils import (
    base_embed,
    error_embed,
    fmt_duration,
    info_embed,
    success_embed,
    truncate,
)

EMOJIS = {
    "Overview": "🏠",
    "Utility": "🛠️",
    "Moderation": "🛡️",
    "Channels": "🔐",
    "Vouches": "⭐",
    "Security": "🚨",
    "AgeCheck": "🕒",
    "Modmail": "📬",
    "TempVoice": "🔊",
    "Birthdays": "🎂",
    "AFK": "💤",
    "Games": "🪙",
    "Counting": "🔢",
    "Tags": "📌",
    "Giveaways": "🎉",
    "Backups": "💾",
    "Verification": "✅",
    "Marketplace": "🛒",
    "Status": "📡",
    "Owner": "👑",
}


def resolve_channel(ctx: commands.Context, token: str) -> discord.TextChannel | None:
    """Match a channel by mention, ID or name inside the command's guild."""
    if ctx.guild is None:
        return None
    if token.startswith("<#") and token.endswith(">"):
        try:
            return ctx.guild.get_channel(int(token[2:-1]))
        except ValueError:
            return None
    if token.isdigit():
        channel = ctx.guild.get_channel(int(token))
        return channel if isinstance(channel, discord.TextChannel) else None
    return discord.utils.get(ctx.guild.text_channels, name=token)


class HelpSelect(discord.ui.Select):
    def __init__(self, author_id: int, pages: dict[str, discord.Embed]) -> None:
        self.author_id = author_id
        self.pages = pages
        super().__init__(
            placeholder="Browse a category…",
            options=[
                discord.SelectOption(
                    label=name, value=name, emoji=EMOJIS.get(name, "📁")
                )
                for name in list(pages)[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Run `help` yourself to browse the commands.", ephemeral=True
            )
            return
        await interaction.response.edit_message(embed=self.pages[self.values[0]])


class HelpView(discord.ui.View):
    def __init__(self, author_id: int, pages: dict[str, discord.Embed]) -> None:
        super().__init__(timeout=180)
        self.add_item(HelpSelect(author_id, pages))


class Utility(commands.Cog):
    """Help, prefix settings, announcements and server information."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ help

    @commands.hybrid_command(name="help", aliases=["h", "commands"], usage="[command]")
    async def help_command(
        self, ctx: commands.Context, *, name: str | None = None
    ) -> None:
        """Show every command, or the details of one command."""
        if name:
            command = self._resolve_command(name)
            if command is None:
                await ctx.reply(
                    embed=error_embed(f"No command called **{name}** exists."),
                    mention_author=False,
                )
                return
            embed = base_embed(
                f"{ctx.prefix}{command.qualified_name}",
                command.help or "No description provided.",
                color=0x5865F2,
            )
            usage = f"{ctx.prefix}{command.qualified_name} {command.signature}".strip()
            embed.add_field(name="Usage", value=f"`{truncate(usage, 1000)}`", inline=False)
            if command.aliases:
                aliases = ", ".join(f"`{a}`" for a in command.aliases)
                embed.add_field(name="Aliases", value=aliases, inline=False)
            if isinstance(command, commands.Group):
                children = ", ".join(f"`{c.name}`" for c in command.commands)
                if children:
                    embed.add_field(
                        name="Subcommands", value=truncate(children, 1000), inline=False
                    )
            embed.set_footer(text="<required>  [optional]")
            await ctx.reply(embed=embed, mention_author=False)
            return

        pages = self._build_pages(ctx)
        view = HelpView(ctx.author.id, pages)
        await ctx.reply(embed=pages["Overview"], view=view, mention_author=False)

    def _resolve_command(self, name: str) -> commands.Command | None:
        parts = name.split()
        command: commands.Command | None = self.bot.get_command(parts[0])
        for part in parts[1:]:
            if isinstance(command, commands.Group):
                command = command.get_command(part)
            else:
                return None
        return command

    def _build_pages(self, ctx: commands.Context) -> dict[str, discord.Embed]:
        overview = base_embed(
            f"📖 {self.bot.user.name} — Command Guide",
            "Select a category below to see its commands.\n"
            f"**Current prefix:** `{ctx.prefix}`\n"
            f"**Servers:** `{len(self.bot.guilds)}` • **Latency:** `{self.bot.latency * 1000:.0f}ms`",
        )
        overview.add_field(
            name="Quick start",
            value=(
                f"`{ctx.prefix}prefix set ?` — change the prefix\n"
                f"`{ctx.prefix}verification setup #channel @role` — post the verify panel\n"
                f"`{ctx.prefix}shop channel #marketplace` — pick the shop channel\n"
                f"`{ctx.prefix}say Hello!` — speak as the bot"
            ),
            inline=False,
        )
        overview.add_field(
            name="Tip",
            value="Arguments in `<angle brackets>` are required, `[square brackets]` are optional.\n"
            "Values separated by `|` let you fill several fields at once.",
            inline=False,
        )

        pages: dict[str, discord.Embed] = {"Overview": overview}
        for cog in self.bot.cogs.values():
            top_level = [c for c in cog.get_commands() if not c.hidden]
            commands_list = [
                child for command in top_level for child in self._walk(command)
            ]
            if not commands_list:
                continue
            lines = []
            for command in commands_list:
                usage = f"{ctx.prefix}{command.qualified_name} {command.signature}".strip()
                lines.append(
                    f"**`{truncate(usage, 160)}`**\n{command.short_doc or 'No description.'}"
                )
            embed = base_embed(
                f"{EMOJIS.get(cog.qualified_name, '📁')} {cog.qualified_name}",
                truncate("\n\n".join(lines), 4000),
            )
            embed.set_footer(text=f"{len(commands_list)} commands • {ctx.prefix}help <command>")
            pages[cog.qualified_name] = embed
        return pages

    @staticmethod
    def _walk(command: commands.Command) -> list[commands.Command]:
        """Include the children of command groups so the help menu lists them."""
        collected: list[commands.Command] = [command]
        if isinstance(command, commands.Group):
            for child in command.commands:
                collected.extend(Utility._walk(child))
        return collected

    # ------------------------------------------------------------------ ping

    @commands.hybrid_command(name="ping", aliases=["latency"])
    async def ping(self, ctx: commands.Context) -> None:
        """Check the bot's latency."""
        start = time.perf_counter()
        message = await ctx.reply(embed=info_embed("Pinging…"), mention_author=False)
        rtt = (time.perf_counter() - start) * 1000
        embed = success_embed(
            f"**WebSocket:** `{self.bot.latency * 1000:.0f}ms`\n"
            f"**Round trip:** `{rtt:.0f}ms`",
            title="🏓 Pong!",
        )
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass

    # --------------------------------------------------------------- prefix

    @commands.hybrid_group(
        name="prefix", invoke_without_command=True, case_insensitive=True
    )
    @commands.guild_only()
    async def prefix(self, ctx: commands.Context) -> None:
        """Show or change the command prefix for this server."""
        current = await self.bot.db.get_prefix(ctx.guild.id)
        embed = info_embed(
            f"**Current prefix:** `{current}`\n"
            f"Change it with `{current}prefix set <new prefix>`.\n"
            f"Reset it with `{current}prefix reset`.",
            title="Prefix",
        )
        await ctx.reply(embed=embed, mention_author=False)

    @prefix.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def prefix_set(self, ctx: commands.Context, new_prefix: str) -> None:
        """Change the prefix used in this server."""
        if len(new_prefix) > 5:
            await ctx.reply(
                embed=error_embed("The prefix can be at most **5** characters."),
                mention_author=False,
            )
            return
        if any(ch.isspace() for ch in new_prefix):
            await ctx.reply(
                embed=error_embed("The prefix cannot contain spaces."),
                mention_author=False,
            )
            return

        data = self.bot.db.guild(ctx.guild.id)
        data["prefix"] = new_prefix
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(
                f"The prefix is now `{new_prefix}`.\nTry `{new_prefix}help`.", title="Prefix updated"
            ),
            mention_author=False,
        )

    @prefix.command(name="reset")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def prefix_reset(self, ctx: commands.Context) -> None:
        """Reset the prefix back to the default."""
        data = self.bot.db.guild(ctx.guild.id)
        data["prefix"] = None
        await self.bot.db.save()
        await ctx.reply(
            embed=success_embed(f"The prefix is back to `{DEFAULT_PREFIX}`."),
            mention_author=False,
        )

    # ------------------------------------------------------------------ say

    @commands.hybrid_command(name="say", aliases=["echo", "announce"])
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def say(self, ctx: commands.Context, *, content: str) -> None:
        """Send a message as the bot. Start with #channel to target one."""
        channel = ctx.channel
        first, separator, rest = content.partition(" ")
        target = resolve_channel(ctx, first) if separator else None
        if target is not None and rest.strip():
            channel, content = target, rest.strip()

        if not content:
            await ctx.reply(
                embed=error_embed("Give me something to say."), mention_author=False
            )
            return
        if not channel.permissions_for(ctx.guild.me).send_messages:
            await ctx.reply(
                embed=error_embed(f"I cannot send messages in {channel.mention}."),
                mention_author=False,
            )
            return

        message = getattr(ctx, "message", None)
        files = [
            await attachment.to_file() for attachment in message.attachments[:10]
        ] if message is not None else []
        try:
            await channel.send(
                content,
                files=files,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=True, replied_user=False
                ),
            )
        except discord.HTTPException as exc:
            await ctx.reply(
                embed=error_embed(f"Discord rejected the message: {exc}"),
                mention_author=False,
            )
            return

        await self.bot.audit(
            ctx.guild,
            title="📢 Message sent as the bot",
            description=f"**Channel:** {channel.mention}\n**By:** {ctx.author.mention}\n\n{truncate(content, 500)}",
            color=0x5865F2,
        )
        try:
            if ctx.message is not None:
                await ctx.message.add_reaction("✅")
        except discord.HTTPException:
            pass

    @commands.hybrid_command(name="embed")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def embed(self, ctx: commands.Context, *, content: str) -> None:
        """Send a rich embed as the bot: `title | description`."""
        title, separator, body = content.partition("|")
        if not separator:
            await ctx.reply(
                embed=error_embed(
                    f"Use `{ctx.prefix}embed Title | Description`.\n"
                    "Optionally start with a channel: `#announcements Title | Description`."
                ),
                mention_author=False,
            )
            return

        channel = ctx.channel
        first, space, rest = title.strip().partition(" ")
        target = resolve_channel(ctx, first) if space else None
        if target is not None and rest.strip():
            channel, title = target, rest

        embed = base_embed(
            truncate(title.strip(), 256) or None, truncate(body.strip(), 4000) or None
        )
        await channel.send(embed=embed)
        await self.bot.audit(
            ctx.guild,
            title="📢 Embed sent as the bot",
            description=f"**Channel:** {channel.mention}\n**By:** {ctx.author.mention}\n\n{truncate(title.strip() or body.strip(), 500)}",
            color=0x5865F2,
        )
        try:
            if ctx.message is not None:
                await ctx.message.add_reaction("✅")
        except discord.HTTPException:
            pass

    # ----------------------------------------------------------- information

    @commands.hybrid_command(name="userinfo", aliases=["whois"])
    @commands.guild_only()
    async def userinfo(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show information about a member."""
        member = member or ctx.author
        embed = base_embed(
            title=f"{member}",
            description=member.mention,
            color=member.color if member.color != discord.Color.default() else 0x5865F2,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=f"`{member.id}`")
        embed.add_field(name="Nickname", value=member.nick or "None")
        embed.add_field(name="Bot", value="Yes" if member.bot else "No")
        embed.add_field(
            name="Created", value=discord.utils.format_dt(member.created_at, "R"), inline=False
        )
        if member.joined_at:
            embed.add_field(
                name="Joined",
                value=discord.utils.format_dt(member.joined_at, "R"),
                inline=False,
            )
        roles = [role.mention for role in reversed(member.roles) if role != ctx.guild.default_role]
        embed.add_field(
            name=f"Roles ({len(roles)})",
            value=truncate(" ".join(roles), 1024) or "None",
            inline=False,
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="dm", usage="<member> <message>")
    @commands.guild_only()
    @is_moderator("manage_messages")
    async def dm(
        self, ctx: commands.Context, member: discord.Member, *, message: str
    ) -> None:
        """Send a direct message to a member of this server."""
        if member.bot:
            await ctx.reply(
                embed=error_embed("I cannot DM bot accounts."), mention_author=False
            )
            return

        embed = discord.Embed(
            title=f"Message from {ctx.guild.name}",
            description=truncate(message, 4000),
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        if ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.url)
        # Keep the staff member anonymous in the member's DM.
        embed.set_footer(text=f"Server staff • {ctx.guild.name}")

        try:
            await member.send(embed=embed)
        except discord.Forbidden:
            await ctx.reply(
                embed=error_embed(
                    f"I could not DM **{member}**. They may have DMs closed "
                    "or only accept DMs from friends."
                ),
                mention_author=False,
            )
            return
        except discord.HTTPException as exc:
            await ctx.reply(
                embed=error_embed(f"Discord rejected the DM: {exc}"),
                mention_author=False,
            )
            return

        await self.bot.audit(
            ctx.guild,
            title="📨 Direct message sent",
            description=(
                f"**To:** {member.mention} (`{member.id}`)\n"
                f"**By:** {ctx.author.mention} (`{ctx.author.id}`)\n"
                f"**Message:** {truncate(message, 800)}"
            ),
            color=0x5865F2,
        )
        await ctx.reply(
            embed=success_embed(
                f"DM sent to **{member}**. Your name was not included.",
                title="📨 Message delivered",
            ),
            mention_author=False,
        )

    @commands.hybrid_command(name="serverinfo", aliases=["guildinfo"])
    @commands.guild_only()
    async def serverinfo(self, ctx: commands.Context) -> None:
        """Show information about this server."""
        guild = ctx.guild
        embed = base_embed(guild.name, guild.description, color=0x5865F2)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if guild.banner:
            embed.set_image(url=guild.banner.url)
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>")
        embed.add_field(name="Members", value=f"`{guild.member_count}`")
        embed.add_field(
            name="Channels",
            value=f"`{len(guild.text_channels)}` text • `{len(guild.voice_channels)}` voice",
        )
        embed.add_field(name="Roles", value=f"`{len(guild.roles)}`")
        embed.add_field(name="Boosts", value=f"`{guild.premium_subscription_count}` (level {guild.premium_tier})")
        embed.add_field(name="ID", value=f"`{guild.id}`")
        embed.add_field(
            name="Created",
            value=discord.utils.format_dt(guild.created_at, "R"),
            inline=False,
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="avatar", aliases=["pfp"])
    async def avatar(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show a member's avatar."""
        member = member or ctx.author
        embed = base_embed(title=f"{member.display_name}'s avatar")
        embed.set_image(url=member.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="membercount", aliases=["members"])
    @commands.guild_only()
    async def membercount(self, ctx: commands.Context) -> None:
        """Show how many humans and bots are in the server."""
        humans = sum(1 for m in ctx.guild.members if not m.bot)
        bots = ctx.guild.member_count - humans
        await ctx.reply(
            embed=info_embed(
                f"**Total:** `{ctx.guild.member_count}`\n**Humans:** `{humans}`\n**Bots:** `{bots}`",
                title="Member count",
            ),
            mention_author=False,
        )

    @commands.hybrid_command(name="uptime", aliases=["stats"])
    async def uptime(self, ctx: commands.Context) -> None:
        """Show how long the bot has been online."""
        delta = discord.utils.utcnow() - self.bot.launch_time
        await ctx.reply(
            embed=info_embed(
                f"**Uptime:** `{fmt_duration(int(delta.total_seconds()))}`\n"
                f"**Servers:** `{len(self.bot.guilds)}`\n"
                f"**Python:** `{platform.python_version()}`\n"
                f"**discord.py:** `{discord.__version__}`",
                title="Bot status",
            ),
            mention_author=False,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Utility(bot))
