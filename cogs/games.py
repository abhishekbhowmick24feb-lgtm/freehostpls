"""Coin-flip duels and the counting channel."""

from __future__ import annotations

import asyncio
import logging
import secrets

import discord
from discord.ext import commands

from core.config import ERROR_COLOR, SUCCESS_COLOR
from core.utils import (
    base_embed,
    error_embed,
    info_embed,
    success_embed,
    warning_embed,
)

log = logging.getLogger(__name__)

HEADS = "heads"
TAILS = "tails"
SIDES = {HEADS: "Heads", TAILS: "Tails"}
SPIN_FRAMES = ("🪙", "🌀", "🪙", "✨", "🪙")


class CoinflipView(discord.ui.View):
    """Lets the challenger call heads or tails, then flips the coin."""

    def __init__(self, author: discord.Member, opponent: discord.Member | None) -> None:
        super().__init__(timeout=120)
        self.author = author
        self.opponent = opponent

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                embed=error_embed(f"Only {self.author.mention} calls this flip."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Heads", emoji="👑", style=discord.ButtonStyle.primary)
    async def heads(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._flip(interaction, HEADS)

    @discord.ui.button(label="Tails", emoji="🪙", style=discord.ButtonStyle.secondary)
    async def tails(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._flip(interaction, TAILS)

    @staticmethod
    def _spin_embed(frame: str) -> discord.Embed:
        return base_embed("🪙 Coin flip", f"{frame}  The coin is in the air...")

    async def _flip(self, interaction: discord.Interaction, choice: str) -> None:
        for item in self.children:
            item.disabled = True
        message = interaction.message
        await interaction.response.edit_message(embed=self._spin_embed(SPIN_FRAMES[0]), view=self)

        for frame in SPIN_FRAMES[1:]:
            await asyncio.sleep(0.45)
            try:
                await message.edit(embed=self._spin_embed(frame))
            except discord.HTTPException:
                break

        await asyncio.sleep(0.55)
        result = secrets.choice((HEADS, TAILS))
        try:
            await message.edit(embed=self._result_embed(choice, result), view=None)
        except discord.HTTPException:
            pass
        self.stop()

    def _result_embed(self, choice: str, result: str) -> discord.Embed:
        other = TAILS if choice == HEADS else HEADS
        author_wins = result == choice

        if self.opponent is None:
            title = f"🪙 {SIDES[result]}!"
            description = (
                f"You called **{SIDES[choice]}** and the coin landed on "
                f"**{SIDES[result]}**.\n"
                + ("**You win!**" if author_wins else "**You lose.**")
            )
        else:
            winner = self.author if author_wins else self.opponent
            title = f"🪙 {SIDES[result]}!"
            description = (
                f"{self.author.mention} called **{SIDES[choice]}** - "
                f"{self.opponent.mention} had **{SIDES[other]}**.\n"
                f"The coin landed on **{SIDES[result]}**.\n"
                f"**{winner.mention} wins!**"
            )

        embed = base_embed(
            title, description, color=SUCCESS_COLOR if author_wins else ERROR_COLOR
        )
        embed.set_footer(text="Fair flip - the side is picked at random")
        return embed


class Games(commands.Cog):
    """Flip a coin on your own or against another member."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(name="coinflip", aliases=["flip"], usage="[opponent]")
    @commands.guild_only()
    @discord.app_commands.describe(
        opponent="Member to challenge; leave it empty to flip against yourself"
    )
    async def coinflip(
        self, ctx: commands.Context, opponent: discord.Member | None = None
    ) -> None:
        """Flip a coin - call heads or tails, alone or against a member."""
        if opponent is not None:
            if opponent.id == ctx.author.id:
                await ctx.reply(
                    embed=error_embed(
                        "Leave `opponent` empty to flip against yourself."
                    ),
                    mention_author=False,
                )
                return
            if opponent.bot:
                await ctx.reply(
                    embed=error_embed("You cannot challenge a bot."),
                    mention_author=False,
                )
                return

        if opponent is None:
            description = "Call **heads** or **tails** and see how the coin lands."
        else:
            description = (
                f"{ctx.author.mention} vs {opponent.mention}\n\n"
                f"{ctx.author.mention} calls a side; {opponent.mention} gets the "
                "other one. The losing side is decided by the coin."
            )

        embed = base_embed("🪙 Coin flip", description)
        embed.set_footer(text=f"Only {ctx.author} can pick a side")
        await ctx.reply(
            embed=embed,
            view=CoinflipView(ctx.author, opponent),
            mention_author=False,
        )


class Counting(commands.Cog):
    """Count numbers in order - one number per member per day."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}

    def _data(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    def _channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = self._data(guild).get("counting_channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    @staticmethod
    def _state(data: dict) -> dict:
        state = data.get("counting")
        if not isinstance(state, dict):
            state = {}
            data["counting"] = state
        state.setdefault("next", 1)
        state.setdefault("day", None)
        if not isinstance(state.get("users"), dict):
            state["users"] = {}
        return state

    @commands.hybrid_group(name="counting", invoke_without_command=True)
    @commands.guild_only()
    async def counting(self, ctx: commands.Context) -> None:
        """Show counting progress and the configured channel."""
        channel = self._channel(ctx.guild)
        if channel is None:
            await ctx.reply(
                embed=info_embed(
                    "No counting channel is set yet.\n"
                    "Choose one with `/counting setup channel:#counting`."
                ),
                mention_author=False,
            )
            return
        next_number = int(self._state(self._data(ctx.guild)).get("next", 1))
        await ctx.reply(
            embed=info_embed(
                f"**Channel:** {channel.mention}\n"
                f"**Next number:** {next_number}\n\n"
                "One number per member per day. Wrong messages are deleted."
            ),
            mention_author=False,
        )

    @counting.command(name="setup", usage="<#channel>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(channel="Channel where members count")
    async def setup(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Pick the counting channel and start again from 1."""
        permissions = channel.permissions_for(ctx.guild.me)
        missing = [
            name
            for name, ok in (
                ("Send Messages", permissions.send_messages),
                ("Manage Messages", permissions.manage_messages),
                ("Read Message History", permissions.read_message_history),
                ("Add Reactions", permissions.add_reactions),
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

        data = self._data(ctx.guild)
        data["counting_channel_id"] = channel.id
        data["counting"] = {"next": 1, "day": None, "users": {}}
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🔢 Counting channel set",
            description=f"Counting starts at **1** in {channel.mention}.",
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"Members now count in {channel.mention}, starting at **1**.\n"
                "Each member can post one number per day; I add ✅ to correct "
                "ones and delete wrong ones after a 5-second warning.",
                title="🔢 Counting channel set",
            ),
            mention_author=False,
        )

    @counting.command(name="reset")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def reset(self, ctx: commands.Context) -> None:
        """Start the count again from 1 and clear today's members."""
        data = self._data(ctx.guild)
        async with self._lock(ctx.guild.id):
            data["counting"] = {"next": 1, "day": None, "users": {}}
            await self.bot.db.save()

        channel = self._channel(ctx.guild)
        if channel is None:
            description = (
                "The count starts again at **1** once a channel is set with "
                "`/counting setup`."
            )
        else:
            description = (
                f"Counting in {channel.mention} starts again at **1**, and "
                "every member can count once today."
            )

        await self.bot.audit(
            ctx.guild,
            title="🔢 Counting reset",
            description=description,
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(description, title="🔢 Counting reset"),
            mention_author=False,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if message.channel.id != self._data(message.guild).get("counting_channel_id"):
            return
        text = message.content.strip()
        if not text:
            return

        async with self._lock(message.guild.id):
            data = self._data(message.guild)
            state = self._state(data)
            today = discord.utils.utcnow().date().isoformat()
            if state.get("day") != today:
                state["day"] = today
                state["users"] = {}
            users = state["users"]
            author_key = str(message.author.id)
            next_number = int(state.get("next", 1))

            if author_key in users:
                await self._reject(
                    message,
                    f"You already counted today. The next number is "
                    f"**{next_number}** - come back tomorrow.",
                )
                return
            if not (text.isascii() and text.isdigit()):
                await self._reject(
                    message, f"Numbers only. The next number is **{next_number}**."
                )
                return
            if int(text) != next_number:
                await self._reject(
                    message,
                    f"Wrong number - you sent **{text}**, the next number is "
                    f"**{next_number}**.",
                )
                return

            users[author_key] = today
            state["next"] = next_number + 1
            await self.bot.db.save()

        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

    @staticmethod
    async def _reject(message: discord.Message, reason: str) -> None:
        """Delete the bad message and leave a warning that expires after 5 s."""
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        try:
            await message.channel.send(
                embed=warning_embed(reason, title="Counting"), delete_after=5
            )
        except discord.HTTPException as exc:
            log.warning("Could not warn in counting channel %s: %s", message.channel.id, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Games(bot))
    await bot.add_cog(Counting(bot))
