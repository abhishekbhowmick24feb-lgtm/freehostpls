"""Button verification: members click once and receive the configured role."""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from core.agecheck import account_age_days, age_problem, min_age_days
from core.config import SUCCESS_COLOR
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


def expand(text: str) -> str:
    return text.replace("\\n", "\n")


def panel_embed(config: dict) -> discord.Embed:
    embed = discord.Embed(
        title=truncate(config.get("title") or "✅ Verify to enter", 256),
        description=truncate(expand(config.get("description") or ""), 4000) or None,
        color=parse_color(config.get("color"), SUCCESS_COLOR),
    )
    image = banner_url(config.get("image"))
    if image:
        embed.set_image(url=image)
    embed.set_footer(text="Click the button below to verify")
    return embed


def panel_view(config: dict) -> "VerifyView":
    return VerifyView(config.get("button_label") or "Verify")


class VerifyView(discord.ui.View):
    """Persistent view: custom_id stays valid across restarts."""

    def __init__(self, label: str = "Verify") -> None:
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=truncate(label or "Verify", 80),
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id="verification:verify",
        )
        button.callback = self._on_click
        self.add_item(button)

    async def _on_click(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Verification only works inside a server.", ephemeral=True
            )
            return

        config = interaction.client.db.guild(guild.id)["verification"]
        role = guild.get_role(config.get("role_id") or 0)
        if role is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "The verification role no longer exists. Please contact the staff."
                ),
                ephemeral=True,
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

        if role in member.roles:
            await interaction.response.send_message(
                embed=info_embed(f"You are already verified — you have {role.mention}."),
                ephemeral=True,
            )
            return

        minimum = min_age_days(interaction.client, guild)
        problem = age_problem(member, minimum)
        if problem:
            await interaction.response.send_message(
                embed=error_embed(problem), ephemeral=True
            )
            await interaction.client.audit(
                guild,
                title="🕒 Verification blocked — account too young",
                description=(
                    f"{member.mention} tried to verify but does not meet the "
                    "account-age rule."
                ),
                color=0xFEE75C,
                fields=[
                    ("Account age", f"{account_age_days(member):g} days", True),
                    ("Minimum", f"{minimum} days", True),
                ],
            )
            return

        me = guild.me
        if not me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                embed=error_embed("I do not have the **Manage Roles** permission."),
                ephemeral=True,
            )
            return
        if role >= me.top_role:
            await interaction.response.send_message(
                embed=error_embed(
                    "My highest role must be above the verification role. Please ask staff to fix the role order."
                ),
                ephemeral=True,
            )
            return
        if role.managed:
            await interaction.response.send_message(
                embed=error_embed("That role is managed by an integration and cannot be assigned."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await member.add_roles(role, reason="Verification panel")
        except discord.Forbidden:
            await interaction.followup.send(
                embed=error_embed("I am not allowed to give you that role."), ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            log.warning("Failed to assign verification role: %s", exc)
            await interaction.followup.send(
                embed=error_embed("Something went wrong while verifying. Try again in a moment."),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=success_embed(
                f"You are verified! You now have {role.mention}. Enjoy the server.", title="✅ Verified"
            ),
            ephemeral=True,
        )


class Verification(commands.Cog):
    """Create a verification panel: choose the channel and the role it grants."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # --------------------------------------------------------------- helpers

    def _config(self, guild: discord.Guild) -> dict:
        return self.bot.db.guild(guild.id)["verification"]

    async def _delete_panel(self, guild: discord.Guild, config: dict) -> None:
        channel = guild.get_channel(config.get("channel_id") or 0)
        if not isinstance(channel, discord.TextChannel):
            return
        if not config.get("message_id"):
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
                embed=panel_embed(config),
                view=panel_view(config),
                attachments=banner_files(config.get("image")),
            )
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    async def _saved(self, ctx: commands.Context, text: str) -> None:
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="✅ Verification panel updated",
            description=text,
            color=0x57F287,
            fields=[("By", ctx.author.mention, True)],
        )
        if not self._config(ctx.guild).get("message_id"):
            suffix = (
                f" Post the panel with `{ctx.prefix}verification setup #channel @role`."
            )
        elif await self._refresh_panel(ctx.guild):
            suffix = " The panel was updated."
        else:
            suffix = f" Run `{ctx.prefix}verification refresh` to update the panel."
        await ctx.reply(embed=success_embed(text + suffix), mention_author=False)

    # ---------------------------------------------------------------- group

    @commands.hybrid_group(
        name="verification",
        aliases=["verify", "v"],
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def verification(self, ctx: commands.Context) -> None:
        """Set up and manage the verification panel."""
        config = self._config(ctx.guild)
        channel = ctx.guild.get_channel(config.get("channel_id") or 0)
        role = ctx.guild.get_role(config.get("role_id") or 0)
        embed = info_embed(
            f"**Channel:** {channel.mention if channel else 'not set'}\n"
            f"**Role:** {role.mention if role else 'not set'}\n"
            f"**Panel message:** {'posted' if config.get('message_id') else 'not posted'}\n\n"
            f"**Setup:** `{ctx.prefix}verification setup #channel @role`\n"
            f"**Look:** `{ctx.prefix}verification title/description/image/button/color`\n"
            f"**Repost:** `{ctx.prefix}verification refresh`",
            title="✅ Verification",
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ---------------------------------------------------------------- setup

    @verification.command(name="setup", usage="<#channel> <@role> [title]")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def setup(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        role: discord.Role,
        *,
        title: str | None = None,
    ) -> None:
        """Post the verification panel in a channel and pick the role it grants."""
        if role.is_default():
            await ctx.reply(
                embed=error_embed("Pick a real role, not @everyone."), mention_author=False
            )
            return
        if role.managed:
            await ctx.reply(
                embed=error_embed("That role is managed by an integration and cannot be assigned."),
                mention_author=False,
            )
            return
        if role >= ctx.guild.me.top_role:
            await ctx.reply(
                embed=error_embed(
                    "Move my role above the verification role in **Server Settings → Roles** first."
                ),
                mention_author=False,
            )
            return

        permissions = channel.permissions_for(ctx.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            await ctx.reply(
                embed=error_embed(
                    f"I need **Send Messages** and **Embed Links** in {channel.mention}."
                ),
                mention_author=False,
            )
            return

        config = self._config(ctx.guild)
        await self._delete_panel(ctx.guild, config)

        config["channel_id"] = channel.id
        config["role_id"] = role.id
        if title:
            config["title"] = truncate(title, 256)

        try:
            message = await channel.send(
                embed=panel_embed(config),
                view=panel_view(config),
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
        await self.bot.audit(
            ctx.guild,
            title="✅ Verification panel posted",
            description=f"Panel posted in {channel.mention}; it grants {role.mention}.",
            color=0x57F287,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"Verification panel posted in {channel.mention}.\n"
                f"Members who click it receive {role.mention}.",
                title="✅ Verification ready",
            ),
            mention_author=False,
        )

    # --------------------------------------------------------------- design

    @verification.command(name="title", usage="<text>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def title(self, ctx: commands.Context, *, text: str) -> None:
        """Change the panel title."""
        self._config(ctx.guild)["title"] = truncate(text, 256)
        await self._saved(ctx, f"Title set to **{truncate(text, 100)}**.")

    @verification.command(name="description", usage="<text>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def description(self, ctx: commands.Context, *, text: str) -> None:
        """Change the panel text. Use \\n for a new line."""
        if text.lower() in {"none", "clear", "reset"}:
            self._config(ctx.guild)["description"] = ""
        else:
            self._config(ctx.guild)["description"] = truncate(expand(text), 4000)
        await self._saved(ctx, "Panel description updated.")

    @verification.command(name="button", usage="<label>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def button(self, ctx: commands.Context, *, label: str) -> None:
        """Change the text shown on the verify button."""
        self._config(ctx.guild)["button_label"] = truncate(label, 80)
        await self._saved(ctx, f"Button label set to **{truncate(label, 80)}**.")

    @verification.command(name="image", usage="<url|none>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def image(
        self,
        ctx: commands.Context,
        *,
        url: str | None = None,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Add or remove the banner image / GIF on the panel."""
        config = self._config(ctx.guild)
        value, error = await banner_from_input(ctx, attachment, url)
        if error is not None:
            await ctx.reply(embed=error_embed(error), mention_author=False)
            return
        config["image"] = value
        await self._saved(ctx, f"Panel image {banner_change(value)}.")

    @verification.command(name="color", usage="<#hex>")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def color(self, ctx: commands.Context, hex_code: str) -> None:
        """Change the panel colour, e.g. #57F287."""
        cleaned = hex_code.strip().lstrip("#")
        if len(cleaned) != 6:
            await ctx.reply(
                embed=error_embed("Use a 6 character hex colour, for example `#57F287`."),
                mention_author=False,
            )
            return
        try:
            int(cleaned, 16)
        except ValueError:
            await ctx.reply(
                embed=error_embed("That is not a valid hex colour."), mention_author=False
            )
            return
        self._config(ctx.guild)["color"] = f"#{cleaned}"
        await self._saved(ctx, f"Panel colour set to `#{cleaned}`.")

    # -------------------------------------------------------------- control

    @verification.command(name="refresh", aliases=["repost"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def refresh(self, ctx: commands.Context) -> None:
        """Re-render the panel with the current settings."""
        config = self._config(ctx.guild)
        if not config.get("message_id"):
            await ctx.reply(
                embed=error_embed(
                    f"No panel exists yet. Run `{ctx.prefix}verification setup #channel @role`."
                ),
                mention_author=False,
            )
            return
        if await self._refresh_panel(ctx.guild):
            await ctx.reply(
                embed=success_embed("Verification panel refreshed."), mention_author=False
            )
        else:
            await ctx.reply(
                embed=error_embed(
                    "I could not find the panel message — it was probably deleted. "
                    f"Run `{ctx.prefix}verification setup` again."
                ),
                mention_author=False,
            )

    @verification.command(name="remove", aliases=["delete"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def remove(self, ctx: commands.Context) -> None:
        """Delete the verification panel and clear its settings."""
        config = self._config(ctx.guild)
        if not config.get("message_id"):
            await ctx.reply(
                embed=info_embed("There is no verification panel to remove."),
                mention_author=False,
            )
            return
        if not await ConfirmView.prompt(
            ctx, "Delete the verification panel? Members will no longer be able to verify."
        ):
            return
        await self._delete_panel(ctx.guild, config)
        config["message_id"] = None
        config["channel_id"] = None
        config["role_id"] = None
        await self.bot.db.save()
        await self.bot.audit(
            ctx.guild,
            title="🗑️ Verification panel removed",
            description="Members can no longer verify through the panel.",
            color=0xED4245,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed("Verification panel removed."), mention_author=False
        )

    @verification.command(name="status")
    @commands.guild_only()
    async def status(self, ctx: commands.Context) -> None:
        """Show the current verification settings."""
        await self.verification(ctx)


def setup(bot: commands.Bot) -> None:
    return bot.add_cog(Verification(bot))
