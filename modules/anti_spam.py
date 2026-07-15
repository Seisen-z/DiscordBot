"""
modules/anti_spam.py
Anti-spam – tracks message frequency per user and applies a configurable
action (warn / timeout / kick / ban) when a threshold is exceeded within
a rolling time window.  Supports per-channel and per-role exemptions.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from modules.utils import load_json, save_json

# ── Constants ─────────────────────────────────────────────────────────────────

ANTISPAM_CONFIG_FILE = "antispam_configs"

# In-memory per-(guild, user) message timestamp tracker (resets on restart, that's fine)
# { guild_id: { user_id: deque([monotonic_float, ...]) } }
_spam_tracker: dict[int, dict[int, deque]] = defaultdict(lambda: defaultdict(deque))

# Prevents actioning the same user twice within a short window
_actioned_recently: dict[tuple[int, int], float] = {}
_ACTION_COOLDOWN_SECONDS = 30.0


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    data = load_json(ANTISPAM_CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_config(data: dict) -> None:
    save_json(ANTISPAM_CONFIG_FILE, data)


def _default_config() -> dict:
    return {
        "enabled": False,
        "threshold": 5,
        "window_seconds": 5,
        "action": "timeout",
        "timeout_minutes": 10,
        "delete_messages": True,
        "log_channel_id": None,
        "exempt_role_ids": [],
        "exempt_channel_ids": [],
        "dm_user_on_action": True,
        "dm_message": "You were actioned in this server for sending too many messages too quickly (spam).",
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


# ── Action executor ───────────────────────────────────────────────────────────

_ACTION_LABELS = {
    "warn": "⚠️ Warned",
    "timeout": "⏱️ Timed Out",
    "kick": "👢 Kicked",
    "ban": "🔨 Banned",
}
_ACTION_COLORS = {
    "warn": discord.Color.yellow(),
    "timeout": discord.Color.gold(),
    "kick": discord.Color.red(),
    "ban": discord.Color.dark_red(),
}


async def _execute_spam_action(
    member: discord.Member,
    channel: discord.abc.Messageable,
    cfg: dict,
    message_count: int,
    spam_messages: list[discord.Message],
) -> None:
    guild = member.guild
    action = str(cfg.get("action", "timeout")).lower()

    # 1. Delete spam messages
    if cfg.get("delete_messages", True):
        for msg in spam_messages:
            try:
                await msg.delete()
            except (discord.Forbidden, discord.NotFound):
                pass

    # 2. DM user
    if cfg.get("dm_user_on_action", True):
        dm_msg = str(cfg.get("dm_message") or "You were actioned for spamming.")
        try:
            await member.send(f"⚠️ **{guild.name}**\n{dm_msg}")
        except Exception:
            pass

    # 3. Apply punishment — administrators are immune to kicks/bans/timeouts
    if not member.guild_permissions.administrator and action != "warn":
        try:
            if action == "timeout":
                minutes = max(1, int(cfg.get("timeout_minutes", 10)))
                until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
                await member.timeout(until, reason="Anti-Spam: excessive message rate")
            elif action == "kick":
                await member.kick(reason="Anti-Spam: excessive message rate")
            elif action == "ban":
                await member.ban(reason="Anti-Spam: excessive message rate", delete_message_days=0)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[AntiSpam] Could not apply '{action}' to {member}: {e}")

    # 4. Log to channel
    log_channel_id = cfg.get("log_channel_id")
    if not log_channel_id:
        return
    try:
        log_ch = guild.get_channel(int(log_channel_id))
        if not (log_ch and hasattr(log_ch, "send")):
            return
        timeout_detail = f" ({cfg.get('timeout_minutes', 10)} min)" if action == "timeout" else ""
        embed = discord.Embed(
            title=f"🚫 Anti-Spam — {_ACTION_LABELS.get(action, action.title())}{timeout_detail}",
            color=_ACTION_COLORS.get(action, discord.Color.orange()),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=True)
        embed.add_field(name="Channel", value=channel.mention if hasattr(channel, "mention") else str(channel), inline=True)
        embed.add_field(name="Action", value=f"{_ACTION_LABELS.get(action, action.title())}{timeout_detail}", inline=True)
        embed.add_field(
            name="Trigger",
            value=f"{message_count} messages within {cfg.get('window_seconds', 5)}s",
            inline=False,
        )
        embed.set_footer(text=f"User ID: {member.id}")
        await log_ch.send(embed=embed)
    except Exception as e:
        print(f"[AntiSpam] Log channel error: {e}")


# ── Event listener ────────────────────────────────────────────────────────────

def _register_listener(bot: commands.Bot) -> None:

    @bot.listen("on_message")
    async def antispam_on_message(message: discord.Message) -> None:
        if not message.guild:
            return
        if message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return

        cfg = get_guild_config(message.guild.id)
        if not cfg.get("enabled", False):
            return

        member: discord.Member = message.author
        guild_id = message.guild.id
        user_id = member.id

        # Exempt channels
        if str(message.channel.id) in [str(c) for c in cfg.get("exempt_channel_ids", [])]:
            return

        # Exempt roles
        exempt_roles = set(str(r) for r in cfg.get("exempt_role_ids", []))
        if exempt_roles & {str(r.id) for r in member.roles}:
            return

        # Track this message's timestamp in the rolling window
        now = time.monotonic()
        window = float(cfg.get("window_seconds", 5))
        threshold = int(cfg.get("threshold", 5))

        user_times = _spam_tracker[guild_id][user_id]
        user_times.append(now)

        # Drop timestamps older than the window
        cutoff = now - window
        while user_times and user_times[0] < cutoff:
            user_times.popleft()

        if len(user_times) < threshold:
            return

        # Avoid actioning the same user twice in quick succession
        cooldown_key = (guild_id, user_id)
        last_action = _actioned_recently.get(cooldown_key, 0.0)
        if now - last_action < _ACTION_COOLDOWN_SECONDS:
            return

        _actioned_recently[cooldown_key] = now
        triggered_count = len(user_times)
        _spam_tracker[guild_id].pop(user_id, None)

        # Collect the user's recent messages in this channel to delete
        spam_msgs: list[discord.Message] = []
        if cfg.get("delete_messages", True):
            try:
                after_dt = datetime.now(timezone.utc) - timedelta(seconds=window + 2)
                async for hist_msg in message.channel.history(limit=50, after=after_dt):
                    if hist_msg.author.id == user_id:
                        spam_msgs.append(hist_msg)
            except Exception:
                spam_msgs = [message]

        await _execute_spam_action(member, message.channel, cfg, triggered_count, spam_msgs)


# ── Slash commands ────────────────────────────────────────────────────────────

def _register_commands(bot: commands.Bot) -> None:

    antispam = app_commands.Group(
        name="antispam",
        description="Anti-spam configuration",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # ── /antispam status ──────────────────────────────────────────────────────

    @antispam.command(name="status", description="Show the current anti-spam configuration")
    async def antispam_status(interaction: discord.Interaction) -> None:
        cfg = get_guild_config(interaction.guild.id)
        action = cfg.get("action", "timeout")
        timeout_detail = f" ({cfg.get('timeout_minutes', 10)} min)" if action == "timeout" else ""

        embed = discord.Embed(
            title="🚫 Anti-Spam Configuration",
            color=discord.Color.blurple() if cfg.get("enabled") else discord.Color.dark_gray(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Status", value="✅ Enabled" if cfg.get("enabled") else "❌ Disabled", inline=True)
        embed.add_field(
            name="Threshold",
            value=f"{cfg.get('threshold', 5)} messages / {cfg.get('window_seconds', 5)}s",
            inline=True,
        )
        embed.add_field(
            name="Action",
            value=f"{_ACTION_LABELS.get(action, action.title())}{timeout_detail}",
            inline=True,
        )
        embed.add_field(name="Delete Spam Messages", value="✅ Yes" if cfg.get("delete_messages") else "❌ No", inline=True)
        embed.add_field(name="DM User on Action", value="✅ Yes" if cfg.get("dm_user_on_action") else "❌ No", inline=True)

        log_ch_id = cfg.get("log_channel_id")
        log_ch = interaction.guild.get_channel(int(log_ch_id)) if log_ch_id else None
        embed.add_field(name="Log Channel", value=log_ch.mention if log_ch else "*(not set)*", inline=True)

        exempt_channels = cfg.get("exempt_channel_ids", [])
        if exempt_channels:
            mentions = []
            for cid in exempt_channels:
                ch = interaction.guild.get_channel(int(cid))
                mentions.append(ch.mention if ch else f"`{cid}`")
            embed.add_field(name="Exempt Channels", value=" ".join(mentions), inline=False)
        else:
            embed.add_field(name="Exempt Channels", value="*(none)*", inline=True)

        exempt_roles = cfg.get("exempt_role_ids", [])
        if exempt_roles:
            mentions = []
            for rid in exempt_roles:
                r = interaction.guild.get_role(int(rid))
                mentions.append(r.mention if r else f"`{rid}`")
            embed.add_field(name="Exempt Roles", value=" ".join(mentions), inline=False)
        else:
            embed.add_field(name="Exempt Roles", value="*(none)*", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /antispam enable ──────────────────────────────────────────────────────

    @antispam.command(name="enable", description="Enable anti-spam for this server")
    async def antispam_enable(interaction: discord.Interaction) -> None:
        cfg = get_guild_config(interaction.guild.id)
        cfg["enabled"] = True
        save_guild_config(interaction.guild.id, cfg)
        await interaction.response.send_message("✅ Anti-spam is now **enabled**.", ephemeral=True)

    # ── /antispam disable ─────────────────────────────────────────────────────

    @antispam.command(name="disable", description="Disable anti-spam for this server")
    async def antispam_disable(interaction: discord.Interaction) -> None:
        cfg = get_guild_config(interaction.guild.id)
        cfg["enabled"] = False
        save_guild_config(interaction.guild.id, cfg)
        await interaction.response.send_message("✅ Anti-spam is now **disabled**.", ephemeral=True)

    # ── /antispam configure ───────────────────────────────────────────────────

    @antispam.command(name="configure", description="Set spam thresholds and action")
    @app_commands.describe(
        threshold="Number of messages that triggers the action (1-20)",
        window="Time window in seconds (1-30)",
        action="Action to take when spam is detected",
        timeout_minutes="Timeout duration in minutes (if action is timeout)",
        delete_messages="Whether to delete the spam messages",
        dm_user="Whether to DM the user when actioned",
    )
    async def antispam_configure(
        interaction: discord.Interaction,
        threshold: app_commands.Range[int, 1, 20] = None,
        window: app_commands.Range[int, 1, 30] = None,
        action: Literal["warn", "timeout", "kick", "ban"] = None,
        timeout_minutes: app_commands.Range[int, 1, 40320] = None,
        delete_messages: bool = None,
        dm_user: bool = None,
    ) -> None:
        cfg = get_guild_config(interaction.guild.id)
        changed = []

        if threshold is not None:
            cfg["threshold"] = threshold
            changed.append(f"threshold → **{threshold} messages**")
        if window is not None:
            cfg["window_seconds"] = window
            changed.append(f"window → **{window}s**")
        if action is not None:
            cfg["action"] = action
            changed.append(f"action → **{action}**")
        if timeout_minutes is not None:
            cfg["timeout_minutes"] = timeout_minutes
            changed.append(f"timeout → **{timeout_minutes} min**")
        if delete_messages is not None:
            cfg["delete_messages"] = delete_messages
            changed.append(f"delete messages → **{'yes' if delete_messages else 'no'}**")
        if dm_user is not None:
            cfg["dm_user_on_action"] = dm_user
            changed.append(f"DM user → **{'yes' if dm_user else 'no'}**")

        if not changed:
            await interaction.response.send_message(
                "ℹ️ No changes made. Provide at least one option to update.", ephemeral=True
            )
            return

        save_guild_config(interaction.guild.id, cfg)
        await interaction.response.send_message(
            "✅ Anti-spam updated:\n" + "\n".join(f"• {c}" for c in changed),
            ephemeral=True,
        )

    # ── /antispam log_channel ─────────────────────────────────────────────────

    @antispam.command(name="log_channel", description="Set (or clear) the channel where spam actions are logged")
    @app_commands.describe(channel="The channel to log anti-spam actions (leave empty to clear)")
    async def antispam_log_channel(
        interaction: discord.Interaction,
        channel: discord.TextChannel = None,
    ) -> None:
        cfg = get_guild_config(interaction.guild.id)
        if channel:
            cfg["log_channel_id"] = channel.id
            save_guild_config(interaction.guild.id, cfg)
            await interaction.response.send_message(
                f"✅ Anti-spam log channel set to {channel.mention}.", ephemeral=True
            )
        else:
            cfg["log_channel_id"] = None
            save_guild_config(interaction.guild.id, cfg)
            await interaction.response.send_message(
                "✅ Anti-spam log channel cleared.", ephemeral=True
            )

    # ── /antispam exempt_channel ──────────────────────────────────────────────

    @antispam.command(name="exempt_channel", description="Add or remove a channel exemption from anti-spam")
    @app_commands.describe(
        action="Whether to add or remove the exemption",
        channel="The channel to exempt or un-exempt",
    )
    async def antispam_exempt_channel(
        interaction: discord.Interaction,
        action: Literal["add", "remove"],
        channel: discord.TextChannel,
    ) -> None:
        cfg = get_guild_config(interaction.guild.id)
        exempt = [str(c) for c in cfg.get("exempt_channel_ids", [])]
        cid = str(channel.id)

        if action == "add":
            if cid in exempt:
                await interaction.response.send_message(
                    f"ℹ️ {channel.mention} is already exempt.", ephemeral=True
                )
                return
            exempt.append(cid)
            cfg["exempt_channel_ids"] = exempt
            save_guild_config(interaction.guild.id, cfg)
            await interaction.response.send_message(
                f"✅ {channel.mention} is now **exempt** from anti-spam.", ephemeral=True
            )
        else:
            if cid not in exempt:
                await interaction.response.send_message(
                    f"ℹ️ {channel.mention} is not in the exempt list.", ephemeral=True
                )
                return
            exempt.remove(cid)
            cfg["exempt_channel_ids"] = exempt
            save_guild_config(interaction.guild.id, cfg)
            await interaction.response.send_message(
                f"✅ {channel.mention} is **no longer exempt** from anti-spam.", ephemeral=True
            )

    # ── /antispam exempt_role ─────────────────────────────────────────────────

    @antispam.command(name="exempt_role", description="Add or remove a role exemption from anti-spam")
    @app_commands.describe(
        action="Whether to add or remove the exemption",
        role="The role to exempt or un-exempt",
    )
    async def antispam_exempt_role(
        interaction: discord.Interaction,
        action: Literal["add", "remove"],
        role: discord.Role,
    ) -> None:
        cfg = get_guild_config(interaction.guild.id)
        exempt = [str(r) for r in cfg.get("exempt_role_ids", [])]
        rid = str(role.id)

        if action == "add":
            if rid in exempt:
                await interaction.response.send_message(
                    f"ℹ️ {role.mention} is already exempt.", ephemeral=True
                )
                return
            exempt.append(rid)
            cfg["exempt_role_ids"] = exempt
            save_guild_config(interaction.guild.id, cfg)
            await interaction.response.send_message(
                f"✅ {role.mention} is now **exempt** from anti-spam.", ephemeral=True
            )
        else:
            if rid not in exempt:
                await interaction.response.send_message(
                    f"ℹ️ {role.mention} is not in the exempt list.", ephemeral=True
                )
                return
            exempt.remove(rid)
            cfg["exempt_role_ids"] = exempt
            save_guild_config(interaction.guild.id, cfg)
            await interaction.response.send_message(
                f"✅ {role.mention} is **no longer exempt** from anti-spam.", ephemeral=True
            )

    bot.tree.add_command(antispam)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: commands.Bot) -> None:
    _register_listener(bot)
    _register_commands(bot)
    print("[System] Anti-spam module loaded")
