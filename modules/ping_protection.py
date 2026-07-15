"""
modules/ping_protection.py
Ping Protection – warns a member when they @mention a protected user
(server administrators and/or a configured list of specific users/roles).

Configuration is dashboard-only (no slash commands) — see the Seisen
Dashboard → Ping Protection page. Config is read live via load_json so
changes take effect without restarting the bot.
"""

from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord.ext import commands

from modules.utils import load_json, save_json

# ── Constants ─────────────────────────────────────────────────────────────────

PINGPROTECT_CONFIG_FILE = "pingprotect_configs"


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    data = load_json(PINGPROTECT_CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_config(data: dict) -> None:
    save_json(PINGPROTECT_CONFIG_FILE, data)


def _default_config() -> dict:
    return {
        "enabled": False,
        "protected_user_ids": [],
        "protect_admins": True,
        "protected_role_ids": [],
        "exempt_role_ids": [],
        "exempt_channel_ids": [],
        "delete_message": False,
        "log_channel_id": None,
        "dm_user_on_warn": True,
        "warn_message": "⚠️ Please avoid unnecessarily pinging staff/admins. This is a warning.",
    }


def get_guild_config(guild_id: int) -> dict:
    base = _default_config()
    stored = _load_config().get(str(guild_id), {})
    base.update(stored)
    return base


def save_guild_config(guild_id: int, cfg: dict) -> None:
    all_cfg = _load_config()
    all_cfg[str(guild_id)] = cfg
    _save_config(all_cfg)


# ── Detection helpers ─────────────────────────────────────────────────────────

def _is_protected_member(target: discord.Member, cfg: dict) -> bool:
    if str(target.id) in {str(v) for v in cfg.get("protected_user_ids", [])}:
        return True
    if cfg.get("protect_admins", True) and target.guild_permissions.administrator:
        return True
    protected_roles = {str(v) for v in cfg.get("protected_role_ids", [])}
    if protected_roles and any(str(r.id) in protected_roles for r in target.roles):
        return True
    return False


# ── Action executor ───────────────────────────────────────────────────────────

async def _execute_warning(
    message: discord.Message,
    cfg: dict,
    target: discord.Member,
) -> None:
    member = message.author
    guild = message.guild

    # 1. Optionally delete the offending message
    if cfg.get("delete_message", False):
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

    # 2. DM the user a warning
    if cfg.get("dm_user_on_warn", True):
        warn_msg = str(cfg.get("warn_message") or _default_config()["warn_message"])
        try:
            await member.send(f"⚠️ **{guild.name}**\n{warn_msg}")
        except Exception:
            pass

    # 3. Log embed
    log_channel_id = cfg.get("log_channel_id")
    if not log_channel_id:
        return
    try:
        log_ch = guild.get_channel(int(log_channel_id))
        if not (log_ch and hasattr(log_ch, "send")):
            return
        embed = discord.Embed(
            title="⚠️ Ping Protection — Warned",
            color=discord.Color.yellow(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=True)
        embed.add_field(name="Protected User Pinged", value=f"{target.mention} (`{target.id}`)", inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        content_preview = (message.content or "*(no text)*")[:512]
        embed.add_field(name="Message Preview", value=f"```\n{content_preview}\n```", inline=False)
        embed.set_footer(text=f"User ID: {member.id}")
        await log_ch.send(embed=embed)
    except Exception as exc:
        print(f"[PingProtection] Log channel error: {exc}")


# ── Event listener ────────────────────────────────────────────────────────────

def _register_listener(bot: commands.Bot) -> None:

    @bot.listen("on_message")
    async def ping_protection_on_message(message: discord.Message) -> None:
        if not message.guild:
            return
        if message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return
        if not message.mentions:
            return

        cfg = get_guild_config(message.guild.id)
        if not cfg.get("enabled", False):
            return

        member: discord.Member = message.author

        # Exempt channels
        if str(message.channel.id) in {str(c) for c in cfg.get("exempt_channel_ids", [])}:
            return

        # Exempt roles (e.g. moderators allowed to ping admins)
        exempt_roles = {str(r) for r in cfg.get("exempt_role_ids", [])}
        if exempt_roles & {str(r.id) for r in member.roles}:
            return

        for target in message.mentions:
            if not isinstance(target, discord.Member):
                continue
            if target.bot or target.id == member.id:
                continue
            if _is_protected_member(target, cfg):
                await _execute_warning(message, cfg, target)
                return


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: commands.Bot) -> None:
    _register_listener(bot)
    print("[System] Ping Protection module loaded")
