"""
modules/trap_channel.py
Restricted "trap" channels: any non-exempt member who sends a message in one of
these channels is immediately kicked or banned (per configuration), and their
recent messages are purged from the server.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord.ext import commands

from modules.utils import load_json

TRAP_CHANNEL_CONFIG_FILE = "trap_channel_configs"

DEFAULT_CONFIG = {
    "enabled": True,
    "channel_ids": [],
    "action": "kick",  # "kick" | "ban"
    "delete_message_hours": 6,
    "exempt_role_ids": [],
    "delete_trigger_message": True,
    "reason": "Posted in a restricted channel",
    "log_channel_id": None,
}


def _guild_cfg(guild_id: int) -> dict:
    root = load_json(TRAP_CHANNEL_CONFIG_FILE, {})
    if not isinstance(root, dict):
        root = {}
    cfg = root.get(str(guild_id), {})
    if not isinstance(cfg, dict):
        cfg = {}
    merged = {**DEFAULT_CONFIG, **cfg}
    merged["enabled"] = bool(merged.get("enabled", True))

    channel_ids = merged.get("channel_ids", [])
    merged["channel_ids"] = [str(c) for c in channel_ids] if isinstance(channel_ids, list) else []

    action = str(merged.get("action", "kick")).strip().lower()
    merged["action"] = action if action in {"kick", "ban"} else "kick"

    merged["delete_message_hours"] = max(0, min(168, int(merged.get("delete_message_hours", 6) or 0)))

    exempt_roles = merged.get("exempt_role_ids", [])
    merged["exempt_role_ids"] = [str(r) for r in exempt_roles] if isinstance(exempt_roles, list) else []

    merged["delete_trigger_message"] = bool(merged.get("delete_trigger_message", True))
    merged["reason"] = str(merged.get("reason") or DEFAULT_CONFIG["reason"])

    log_ch = merged.get("log_channel_id")
    merged["log_channel_id"] = str(log_ch) if log_ch else None
    return merged


def _channel_and_parent_ids(channel: Any) -> set[str]:
    ids = {str(channel.id)}
    parent = getattr(channel, "parent", None)
    if parent is not None:
        ids.add(str(parent.id))
    return ids


async def _purge_member_messages(guild: discord.Guild, user_id: int, cutoff: datetime) -> int:
    """Best-effort delete of a member's recent messages across every channel the bot can manage."""
    deleted = 0
    channels: list[Any] = list(guild.text_channels) + list(guild.voice_channels) + list(guild.threads)
    for channel in channels:
        perms = channel.permissions_for(guild.me)
        if not (perms.manage_messages and perms.read_message_history):
            continue
        try:
            deleted_msgs = await channel.purge(limit=200, after=cutoff, check=lambda m: m.author.id == user_id)
            deleted += len(deleted_msgs)
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            continue
    return deleted


async def _log_action(
    guild: discord.Guild,
    cfg: dict,
    member: discord.Member,
    message: discord.Message,
    action: str,
    purged: int,
) -> None:
    log_channel_id = cfg.get("log_channel_id")
    if not log_channel_id:
        return
    try:
        log_ch = guild.get_channel(int(log_channel_id))
    except (TypeError, ValueError):
        log_ch = None
    if not log_ch or not hasattr(log_ch, "send"):
        return

    action_labels = {"kick": "👢 Kicked", "ban": "🔨 Banned"}
    embed = discord.Embed(
        title=f"🚨 Trap Channel Triggered — {action_labels.get(action, action.title())}",
        color=discord.Color.dark_red() if action == "ban" else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=True)
    channel_display = message.channel.mention if hasattr(message.channel, "mention") else str(message.channel)
    embed.add_field(name="Channel", value=channel_display, inline=True)
    embed.add_field(name="Messages Purged", value=str(purged), inline=True)
    content_preview = (message.content or "*(no text)*")[:512]
    embed.add_field(name="Message Preview", value=f"```\n{content_preview}\n```", inline=False)
    try:
        await log_ch.send(embed=embed)
    except discord.HTTPException:
        pass


async def _handle_trap_trigger(message: discord.Message) -> None:
    if not message.guild or message.author.bot:
        return
    if not isinstance(message.author, discord.Member):
        return

    cfg = _guild_cfg(message.guild.id)
    if not cfg.get("enabled", True) or not cfg.get("channel_ids"):
        return

    if not _channel_and_parent_ids(message.channel) & set(cfg["channel_ids"]):
        return

    member = message.author
    guild = message.guild

    exempt_roles = set(cfg.get("exempt_role_ids", []))
    member_role_ids = {str(r.id) for r in member.roles}
    if exempt_roles & member_role_ids:
        return

    if cfg.get("delete_trigger_message", True):
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

    if member.guild_permissions.administrator:
        try:
            await member.send(
                f"*(Note: your message in **{guild.name}** triggered the trap-channel rule and would have resulted "
                f"in a **{cfg['action']}**, but you were spared because you're a Server Administrator.)*"
            )
        except discord.HTTPException:
            pass
        return

    hours = int(cfg.get("delete_message_hours", 6))
    reason = str(cfg.get("reason") or DEFAULT_CONFIG["reason"])
    action = cfg.get("action", "kick")
    purged = 0

    try:
        if action == "ban":
            seconds = min(604800, max(0, hours * 3600))
            await member.ban(reason=reason, delete_message_seconds=seconds)
        else:
            await member.kick(reason=reason)
            if hours > 0:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
                purged = await _purge_member_messages(guild, member.id, cutoff)
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"[TrapChannel] Could not {action} {member} in guild {guild.id}: {e}")
        return

    await _log_action(guild, cfg, member, message, action, purged)


def register(bot: commands.Bot):
    bot.add_listener(_handle_trap_trigger, "on_message")
