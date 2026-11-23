"""Role, channel and permission backups for recovering from a nuke."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands

from core.config import DATA_DIR, SUCCESS_COLOR, WARNING_COLOR
from core.utils import (
    ConfirmView,
    error_embed,
    info_embed,
    success_embed,
    truncate,
    warning_embed,
)

log = logging.getLogger(__name__)

BACKUP_ROOT = DATA_DIR / "backups"
BACKUP_VERSION = 1


class Backups(commands.Cog):
    """Create and restore snapshots of roles, channels and permissions."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _folder(guild: discord.Guild) -> Path:
        return BACKUP_ROOT / str(guild.id)

    def _path(self, guild: discord.Guild, backup_id: str) -> Path | None:
        clean = backup_id.strip()
        if clean.endswith(".json"):
            clean = clean[:-5]
        if not clean or Path(clean).name != clean:
            return None
        path = self._folder(guild) / f"{clean}.json"
        return path if path.is_file() else None

    @staticmethod
    def _channel_type(channel: discord.abc.GuildChannel) -> str:
        if isinstance(channel, discord.CategoryChannel):
            return "category"
        if isinstance(channel, discord.ForumChannel):
            return "forum"
        if isinstance(channel, discord.StageChannel):
            return "stage"
        if isinstance(channel, discord.VoiceChannel):
            return "voice"
        if isinstance(channel, discord.TextChannel):
            return "news" if channel.is_news() else "text"
        return "other"

    @staticmethod
    def _serialize_overwrites(
        channel: discord.abc.GuildChannel,
    ) -> list[dict]:
        result: list[dict] = []
        for target, overwrite in channel.overwrites.items():
            if isinstance(target, discord.Role):
                kind = "role"
            elif isinstance(target, discord.Member):
                kind = "member"
            else:
                kind = "unknown"
            allow, deny = overwrite.pair()
            result.append(
                {
                    "id": target.id,
                    "type": kind,
                    "allow": allow.value,
                    "deny": deny.value,
                }
            )
        return result

    def _snapshot(self, guild: discord.Guild) -> dict:
        roles: list[dict] = []
        for role in guild.roles:
            roles.append(
                {
                    "id": role.id,
                    "name": role.name,
                    "color": role.colour.value,
                    "hoist": role.hoist,
                    "mentionable": role.mentionable,
                    "permissions": role.permissions.value,
                    "position": role.position,
                    "managed": role.managed,
                    "is_default": role.is_default(),
                }
            )

        channels: list[dict] = []
        for channel in guild.channels:
            item: dict = {
                "id": channel.id,
                "name": channel.name,
                "type": self._channel_type(channel),
                "position": channel.position,
                "category_id": channel.category_id,
                "overwrites": self._serialize_overwrites(channel),
            }
            if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                item["topic"] = getattr(channel, "topic", "") or ""
                item["nsfw"] = bool(getattr(channel, "nsfw", False))
                item["slowmode_delay"] = int(
                    getattr(channel, "slowmode_delay", 0) or 0
                )
            elif isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                item["bitrate"] = int(getattr(channel, "bitrate", 64000) or 64000)
                item["user_limit"] = int(getattr(channel, "user_limit", 0) or 0)
                item["nsfw"] = bool(getattr(channel, "nsfw", False))
            channels.append(item)

        return {
            "version": BACKUP_VERSION,
            "guild_id": guild.id,
            "guild_name": guild.name,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "roles": roles,
            "channels": channels,
        }

    @staticmethod
    def _target(
        guild: discord.Guild,
        item: dict,
        role_map: dict[int, discord.Role],
    ) -> discord.Role | discord.Member | None:
        try:
            target_id = int(item.get("id", 0))
        except (TypeError, ValueError):
            return None
        kind = item.get("type")
        if kind == "role":
            if target_id == guild.id:
                return guild.default_role
            return role_map.get(target_id) or guild.get_role(target_id)
        if kind == "member":
            return guild.get_member(target_id)
        return None

    def _desired_overwrites(
        self,
        saved: list[dict],
        guild: discord.Guild,
        role_map: dict[int, discord.Role],
    ) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
        result: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        for item in saved or []:
            target = self._target(guild, item, role_map)
            if target is None:
                continue
            try:
                allow = discord.Permissions(int(item.get("allow", 0)))
                deny = discord.Permissions(int(item.get("deny", 0)))
            except (TypeError, ValueError):
                continue
            result[target] = discord.PermissionOverwrite.from_pair(allow, deny)
        return result

    async def _sync_overwrites(
        self,
        channel: discord.abc.GuildChannel,
        saved: list[dict],
        guild: discord.Guild,
        role_map: dict[int, discord.Role],
        errors: list[str],
    ) -> None:
        desired = self._desired_overwrites(saved, guild, role_map)
        protected = set()
        if guild.me is not None:
            protected.add(guild.me)
            protected.update(guild.me.roles)
        for target in list(channel.overwrites):
            if target in protected:
                continue
            if target not in desired:
                try:
                    await channel.set_permissions(
                        target, overwrite=None, reason="Backup restore"
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    errors.append(f"{channel.name}: remove overwrite: {exc}")
        for target, overwrite in desired.items():
            try:
                await channel.set_permissions(
                    target, overwrite=overwrite, reason="Backup restore"
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                errors.append(f"{channel.name}: set overwrite: {exc}")

    async def _restore_roles(
        self,
        guild: discord.Guild,
        saved_roles: list[dict],
        errors: list[str],
    ) -> dict[int, discord.Role]:
        role_map: dict[int, discord.Role] = {guild.id: guild.default_role}
        me = guild.me
        if me is None:
            return role_map
        restore_everyone = (
            me.top_role != guild.default_role
            and me.top_role.permissions.administrator
        )

        for item in sorted(
            saved_roles, key=lambda role: int(role.get("position", 0))
        ):
            if item.get("is_default"):
                if restore_everyone:
                    try:
                        await guild.default_role.edit(
                            permissions=discord.Permissions(
                                int(item.get("permissions", 0))
                            ),
                            reason="Backup restore",
                        )
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        errors.append(f"@everyone permissions: {exc}")
                else:
                    errors.append(
                        "@everyone permissions were not restored because my "
                        "top role does not have Administrator."
                    )
                continue
            try:
                old_id = int(item.get("id", 0))
            except (TypeError, ValueError):
                continue
            if not old_id:
                continue

            existing = guild.get_role(old_id)
            if item.get("managed"):
                if existing is not None:
                    role_map[old_id] = existing
                continue

            permissions = discord.Permissions(int(item.get("permissions", 0)))
            colour = discord.Colour(int(item.get("color", 0)))
            name = str(item.get("name", "restored-role"))
            hoist = bool(item.get("hoist", False))
            mentionable = bool(item.get("mentionable", False))

            if existing is not None:
                role_map[old_id] = existing
                if (
                    existing.managed
                    or existing in me.roles
                    or existing >= me.top_role
                ):
                    continue
                try:
                    await existing.edit(
                        name=name,
                        permissions=permissions,
                        colour=colour,
                        hoist=hoist,
                        mentionable=mentionable,
                        reason="Backup restore",
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    errors.append(f"Role {name}: {exc}")
                continue

            try:
                created = await guild.create_role(
                    name=name,
                    permissions=permissions,
                    colour=colour,
                    hoist=hoist,
                    mentionable=mentionable,
                    reason="Backup restore",
                )
                role_map[old_id] = created
                try:
                    await created.edit(
                        position=int(item.get("position", 0)),
                        reason="Backup restore",
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    errors.append(f"Role {name} position: {exc}")
            except (discord.Forbidden, discord.HTTPException) as exc:
                errors.append(f"Role {name}: {exc}")

        return role_map

    async def _restore_channels(
        self,
        guild: discord.Guild,
        saved_channels: list[dict],
        role_map: dict[int, discord.Role],
        errors: list[str],
    ) -> dict[int, discord.abc.GuildChannel]:
        channel_map: dict[int, discord.abc.GuildChannel] = {}
        categories = [
            item for item in saved_channels if item.get("type") == "category"
        ]
        others = [
            item for item in saved_channels if item.get("type") != "category"
        ]

        for item in sorted(
            categories, key=lambda channel: int(channel.get("position", 0))
        ):
            try:
                old_id = int(item.get("id", 0))
            except (TypeError, ValueError):
                continue
            name = str(item.get("name", "restored-category"))
            existing = guild.get_channel(old_id)
            if isinstance(existing, discord.CategoryChannel):
                channel_map[old_id] = existing
                try:
                    await existing.edit(
                        name=name,
                        position=int(item.get("position", 0)),
                        reason="Backup restore",
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    errors.append(f"Category {name}: {exc}")
                await self._sync_overwrites(
                    existing, item.get("overwrites", []), guild, role_map, errors
                )
                continue

            overwrites = self._desired_overwrites(
                item.get("overwrites", []), guild, role_map
            )
            try:
                created = await guild.create_category(
                    name,
                    overwrites=overwrites,
                    position=int(item.get("position", 0)),
                    reason="Backup restore",
                )
                channel_map[old_id] = created
            except (discord.Forbidden, discord.HTTPException) as exc:
                errors.append(f"Category {name}: {exc}")

        for item in sorted(
            others, key=lambda channel: int(channel.get("position", 0))
        ):
            try:
                old_id = int(item.get("id", 0))
            except (TypeError, ValueError):
                continue
            kind = item.get("type")
            name = str(item.get("name", "restored-channel"))
            category_id = item.get("category_id")
            category = (
                channel_map.get(int(category_id))
                if category_id is not None
                else None
            )
            if not isinstance(category, discord.CategoryChannel):
                category = None

            existing = guild.get_channel(old_id)
            if kind in {"text", "news"} and isinstance(existing, discord.TextChannel):
                try:
                    await existing.edit(
                        name=name,
                        position=int(item.get("position", 0)),
                        category=category,
                        topic=item.get("topic", "") or "",
                        nsfw=bool(item.get("nsfw", False)),
                        slowmode_delay=int(item.get("slowmode_delay", 0) or 0),
                        reason="Backup restore",
                    )
                    channel_map[old_id] = existing
                    await self._sync_overwrites(
                        existing, item.get("overwrites", []), guild, role_map, errors
                    )
                except (discord.Forbidden, discord.HTTPException, TypeError) as exc:
                    errors.append(f"Channel {name}: {exc}")
                continue

            if kind == "voice" and isinstance(existing, discord.VoiceChannel):
                try:
                    await existing.edit(
                        name=name,
                        position=int(item.get("position", 0)),
                        category=category,
                        bitrate=int(item.get("bitrate", 64000) or 64000),
                        user_limit=int(item.get("user_limit", 0) or 0),
                        nsfw=bool(item.get("nsfw", False)),
                        reason="Backup restore",
                    )
                    channel_map[old_id] = existing
                    await self._sync_overwrites(
                        existing, item.get("overwrites", []), guild, role_map, errors
                    )
                except (discord.Forbidden, discord.HTTPException, TypeError) as exc:
                    errors.append(f"Channel {name}: {exc}")
                continue

            if kind == "stage" and isinstance(existing, discord.StageChannel):
                try:
                    await existing.edit(
                        name=name,
                        position=int(item.get("position", 0)),
                        category=category,
                        bitrate=int(item.get("bitrate", 64000) or 64000),
                        user_limit=int(item.get("user_limit", 0) or 0),
                        nsfw=bool(item.get("nsfw", False)),
                        reason="Backup restore",
                    )
                    channel_map[old_id] = existing
                    await self._sync_overwrites(
                        existing, item.get("overwrites", []), guild, role_map, errors
                    )
                except (discord.Forbidden, discord.HTTPException, TypeError) as exc:
                    errors.append(f"Channel {name}: {exc}")
                continue

            if kind == "forum" and isinstance(existing, discord.ForumChannel):
                try:
                    await existing.edit(
                        name=name,
                        position=int(item.get("position", 0)),
                        category=category,
                        topic=item.get("topic", "") or "",
                        nsfw=bool(item.get("nsfw", False)),
                        slowmode_delay=int(item.get("slowmode_delay", 0) or 0),
                        reason="Backup restore",
                    )
                    channel_map[old_id] = existing
                    await self._sync_overwrites(
                        existing, item.get("overwrites", []), guild, role_map, errors
                    )
                except (discord.Forbidden, discord.HTTPException, TypeError) as exc:
                    errors.append(f"Channel {name}: {exc}")
                continue

            overwrites = self._desired_overwrites(
                item.get("overwrites", []), guild, role_map
            )
            try:
                if kind in {"text", "news"}:
                    created = await guild.create_text_channel(
                        name,
                        category=category,
                        overwrites=overwrites,
                        topic=item.get("topic", "") or "",
                        nsfw=bool(item.get("nsfw", False)),
                        slowmode_delay=int(item.get("slowmode_delay", 0) or 0),
                        position=int(item.get("position", 0)),
                        news=(kind == "news"),
                        reason="Backup restore",
                    )
                elif kind == "voice":
                    created = await guild.create_voice_channel(
                        name,
                        category=category,
                        overwrites=overwrites,
                        bitrate=int(item.get("bitrate", 64000) or 64000),
                        user_limit=int(item.get("user_limit", 0) or 0),
                        nsfw=bool(item.get("nsfw", False)),
                        position=int(item.get("position", 0)),
                        reason="Backup restore",
                    )
                elif kind == "stage":
                    created = await guild.create_stage_channel(
                        name,
                        category=category,
                        overwrites=overwrites,
                        bitrate=int(item.get("bitrate", 64000) or 64000),
                        user_limit=int(item.get("user_limit", 0) or 0),
                        nsfw=bool(item.get("nsfw", False)),
                        position=int(item.get("position", 0)),
                        reason="Backup restore",
                    )
                elif kind == "forum":
                    created = await guild.create_forum(
                        name,
                        category=category,
                        overwrites=overwrites,
                        topic=item.get("topic", "") or "",
                        nsfw=bool(item.get("nsfw", False)),
                        slowmode_delay=int(item.get("slowmode_delay", 0) or 0),
                        position=int(item.get("position", 0)),
                        reason="Backup restore",
                    )
                else:
                    continue
                channel_map[old_id] = created
            except (discord.Forbidden, discord.HTTPException, TypeError) as exc:
                errors.append(f"Channel {name}: {exc}")

        return channel_map

    async def _reply(self, ctx: commands.Context, **kwargs) -> None:
        """Reply normally, or use a followup after a slash confirmation."""
        if ctx.interaction is not None:
            if ctx.interaction.response.is_done():
                await ctx.interaction.followup.send(**kwargs)
            else:
                await ctx.interaction.response.send_message(**kwargs)
            return
        await ctx.reply(**kwargs)

    # --------------------------------------------------------------- commands

    @commands.hybrid_group(
        name="backup",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def backup(self, ctx: commands.Context) -> None:
        """Save and restore roles, channels and permissions."""
        folder = self._folder(ctx.guild)
        count = len(list(folder.glob("*.json"))) if folder.is_dir() else 0
        await ctx.reply(
            embed=info_embed(
                f"**Saved backups:** `{count}`\n\n"
                f"**Create:** `{ctx.prefix}backup create [name]`\n"
                f"**List:** `{ctx.prefix}backup list`\n"
                f"**Restore:** `{ctx.prefix}backup restore <id>`\n\n"
                "Backups include roles, channels and permission overwrites. "
                "Messages and members are not restored; the bot's own roles "
                "are left untouched.",
                title="💾 Backups",
            ),
            mention_author=False,
        )

    @backup.command(name="create", usage="[name]")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def create(
        self, ctx: commands.Context, *, name: str | None = None
    ) -> None:
        """Save a snapshot of the server's roles, channels and permissions."""
        snapshot = self._snapshot(ctx.guild)
        folder = self._folder(ctx.guild)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            await ctx.reply(
                embed=error_embed(f"Could not create the backup folder: {exc}"),
                mention_author=False,
            )
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", name or "").strip("-")[:40]
        filename = f"{stamp}-{safe}.json" if safe else f"{stamp}.json"
        path = folder / filename
        if path.exists():
            path = folder / f"{path.stem}-1.json"

        try:
            path.write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            await ctx.reply(
                embed=error_embed(f"Could not write the backup: {exc}"),
                mention_author=False,
            )
            return

        await self.bot.audit(
            ctx.guild,
            title="💾 Backup created",
            description=(
                f"**ID:** `{path.stem}`\n"
                f"**Roles:** `{len(snapshot['roles'])}` • "
                f"**Channels:** `{len(snapshot['channels'])}`"
            ),
            color=SUCCESS_COLOR,
            fields=[("By", ctx.author.mention, True)],
        )
        await ctx.reply(
            embed=success_embed(
                f"Saved backup **`{path.stem}`**.\n"
                f"**Roles:** `{len(snapshot['roles'])}`\n"
                f"**Channels:** `{len(snapshot['channels'])}`\n\n"
                f"Restore it with `{ctx.prefix}backup restore {path.stem}`.",
                title="💾 Backup created",
            ),
            mention_author=False,
        )

    @backup.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def list_backups(self, ctx: commands.Context) -> None:
        """List the saved backups for this server."""
        folder = self._folder(ctx.guild)
        files = (
            sorted(
                folder.glob("*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:20]
            if folder.is_dir()
            else []
        )
        if not files:
            await ctx.reply(
                embed=info_embed(
                    "No backups yet.\n"
                    f"Create one with `{ctx.prefix}backup create`."
                ),
                mention_author=False,
            )
            return

        lines: list[str] = []
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            created = str(data.get("created_at", "unknown"))
            roles = len(data.get("roles", []))
            channels = len(data.get("channels", []))
            lines.append(
                f"`{path.stem}`\n"
                f"  {created} • {roles} roles • {channels} channels"
            )
        await ctx.reply(
            embed=info_embed(
                "\n\n".join(lines) or "No readable backups found.",
                title="💾 Saved backups",
            ),
            mention_author=False,
        )

    @backup.command(name="restore", usage="<backup id>")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_roles=True, manage_channels=True)
    @discord.app_commands.describe(backup_id="Backup ID shown by /backup list")
    async def restore(
        self, ctx: commands.Context, backup_id: str
    ) -> None:
        """Restore roles, channels and permissions from a backup."""
        path = self._path(ctx.guild, backup_id)
        if path is None:
            await ctx.reply(
                embed=error_embed(
                    f"Backup `{backup_id}` was not found. Use `{ctx.prefix}backup list`."
                ),
                mention_author=False,
            )
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            await ctx.reply(
                embed=error_embed(f"Could not read that backup: {exc}"),
                mention_author=False,
            )
            return

        if int(data.get("guild_id", 0)) != ctx.guild.id:
            await ctx.reply(
                embed=error_embed("That backup belongs to a different server."),
                mention_author=False,
            )
            return

        confirmed = await ConfirmView.prompt(
            ctx,
            f"Restore backup **`{path.stem}`**?\n\n"
            "Missing roles and channels will be created, and permissions will "
            "be overwritten from the snapshot.\n"
            "Messages are not restored. The @everyone role's permissions are "
            "restored only when my top role has Administrator; the bot's own "
            "roles are left untouched.",
            confirm_label="Restore backup",
        )
        if not confirmed:
            await self._reply(ctx, embed=info_embed("Restore cancelled."))
            return

        await self._reply(
            ctx,
            embed=info_embed(
                "Restoring roles and channels… this can take a while on a large server."
            ),
        )

        errors: list[str] = []
        role_map = await self._restore_roles(
            ctx.guild, data.get("roles", []), errors
        )
        channel_map = await self._restore_channels(
            ctx.guild, data.get("channels", []), role_map, errors
        )

        role_count = max(0, len(role_map) - 1)
        channel_count = len(channel_map)
        summary = (
            f"Restored **{role_count}** role(s) and "
            f"**{channel_count}** channel(s) from `{path.stem}`."
        )
        if errors:
            summary += "\n\n**Some items could not be restored:**\n" + truncate(
                "\n".join(f"• {error}" for error in errors[:10]),
                1024,
            )
            embed = warning_embed(summary, title="💾 Backup restored with warnings")
        else:
            embed = success_embed(summary, title="💾 Backup restored")

        await self.bot.audit(
            ctx.guild,
            title="💾 Backup restored",
            description=summary,
            color=WARNING_COLOR if errors else SUCCESS_COLOR,
            fields=[
                ("Backup", f"`{path.stem}`", True),
                ("By", ctx.author.mention, True),
                ("Errors", f"`{len(errors)}`", True),
            ],
        )
        await self._reply(ctx, embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Backups(bot))
