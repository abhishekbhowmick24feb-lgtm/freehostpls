"""Owner-only tools: reload cogs, inspect servers, debug and shut down."""

from __future__ import annotations

import asyncio
import logging
import platform
import traceback

import discord
from discord.ext import commands

from core.config import EXTENSIONS
from core.utils import ConfirmView, error_embed, info_embed, success_embed, truncate

log = logging.getLogger(__name__)


class Owner(commands.Cog, command_attrs={"hidden": True}):
    """Bot owner tools. Only the application owner can use these."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if not await self.bot.is_owner(ctx.author):
            raise commands.NotOwner("This command is restricted to the bot owner.")
        return True

    @commands.command(name="reload", usage="<cog>")
    async def reload(self, ctx: commands.Context, extension: str) -> None:
        """Reload a single cog, e.g. `reload marketplace`."""
        name = extension if extension.startswith("cogs.") else f"cogs.{extension}"
        try:
            await self.bot.reload_extension(name)
        except commands.ExtensionError as exc:
            await ctx.reply(embed=error_embed(f"`{exc}`"), mention_author=False)
            return
        await ctx.reply(
            embed=success_embed(f"Reloaded `{name}`."), mention_author=False
        )

    @commands.command(name="reloadall")
    async def reloadall(self, ctx: commands.Context) -> None:
        """Reload every cog."""
        failed: list[str] = []
        for name in EXTENSIONS:
            try:
                await self.bot.reload_extension(name)
            except commands.ExtensionError as exc:
                failed.append(f"`{name}` — {exc}")
        if failed:
            await ctx.reply(
                embed=error_embed("\n".join(failed), title="Some cogs failed"),
                mention_author=False,
            )
            return
        await ctx.reply(
            embed=success_embed(f"Reloaded **{len(EXTENSIONS)}** cogs."),
            mention_author=False,
        )

    @commands.command(name="sync")
    async def sync(self, ctx: commands.Context) -> None:
        """Re-sync the slash commands for every server."""
        await self.bot.http.bulk_upsert_global_commands(self.bot.application_id, [])
        for guild in self.bot.guilds:
            await self.bot.sync_guild_commands(guild)
        await ctx.reply(
            embed=success_embed(
                f"Re-synced slash commands for **{len(self.bot.guilds)}** server(s)."
            ),
            mention_author=False,
        )

    @commands.command(name="servers", aliases=["guilds"])
    async def servers(self, ctx: commands.Context) -> None:
        """List the servers the bot is in."""
        guilds = sorted(self.bot.guilds, key=lambda g: len(g.members), reverse=True)
        lines = [
            f"**{truncate(guild.name, 40)}** • `{guild.id}` • `{guild.member_count}` members"
            for guild in guilds[:20]
        ]
        extra = f"\n…and **{len(guilds) - 20}** more." if len(guilds) > 20 else ""
        await ctx.reply(
            embed=info_embed(
                "\n".join(lines) + extra or "No servers.",
                title=f"Servers ({len(guilds)})",
            ),
            mention_author=False,
        )

    @commands.command(name="leave", usage="<guild id>")
    async def leave(self, ctx: commands.Context, guild_id: int) -> None:
        """Make the bot leave a server."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await ctx.reply(
                embed=error_embed("I am not in that server."), mention_author=False
            )
            return
        if not await ConfirmView.prompt(
            ctx, f"Leave **{guild.name}** (`{guild.id}`)?", confirm_label="Leave"
        ):
            return
        await guild.leave()
        await ctx.reply(
            embed=success_embed(f"Left **{guild.name}**."), mention_author=False
        )

    @commands.command(name="shutdown", aliases=["stop"])
    async def shutdown(self, ctx: commands.Context) -> None:
        """Stop the bot process."""
        if not await ConfirmView.prompt(ctx, "Shut the bot down? It will stay offline until restarted."):
            return
        await ctx.reply(embed=success_embed("Shutting down…"), mention_author=False)
        await self.bot.close()

    @commands.command(name="eval", hidden=True)
    async def evaluate(self, ctx: commands.Context, *, code: str) -> None:
        """Run Python code (owner only, be careful)."""
        code = code.strip("` \n")
        if code.startswith("py\n"):
            code = code[3:]
        environment = {
            "bot": self.bot,
            "ctx": ctx,
            "db": self.bot.db,
            "discord": discord,
            "commands": commands,
            "asyncio": asyncio,
        }
        try:
            result = eval(code, environment)  # noqa: S307 - owner only
            if asyncio.iscoroutine(result):
                result = await result
            output = repr(result)
        except Exception:
            output = traceback.format_exc()
        for index in range(0, len(output), 1900):
            await ctx.send(f"```py\n{output[index:index + 1900]}\n```")

    @commands.command(name="botstats", aliases=["sysinfo"])
    async def botstats(self, ctx: commands.Context) -> None:
        """Show hosting and library details."""
        delta = discord.utils.utcnow() - self.bot.launch_time
        embed = info_embed(
            f"**Servers:** `{len(self.bot.guilds)}`\n"
            f"**Users:** `{sum(g.member_count or 0 for g in self.bot.guilds)}`\n"
            f"**Cogs:** `{len(self.bot.cogs)}`\n"
            f"**Latency:** `{self.bot.latency * 1000:.0f}ms`\n"
            f"**Uptime:** `{delta}`\n"
            f"**Python:** `{platform.python_version()}`\n"
            f"**discord.py:** `{discord.__version__}`",
            title="Bot statistics",
        )
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Owner(bot))
