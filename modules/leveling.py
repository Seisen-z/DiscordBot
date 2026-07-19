"""
modules/leveling.py
Message-count based leveling system. Members who chat in tracked channels build up a
message count; crossing a configured threshold grants them a role ("promotion").
Excluded channels never count toward level progress.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord import app_commands

from modules.utils import load_json, save_json

_bot: discord.ext.commands.Bot | None = None

LEVELING_CONFIG_FILE = "leveling_configs"
LEVELING_STATS_FILE = "leveling_stats"

DEFAULT_CONFIG = {
    "enabled": True,
    "excluded_channel_ids": [],
    "cooldown_seconds": 5,
    "announce_level_up": True,
    "announce_channel_id": None,
    "level_up_message": "🎉 {mention} just reached **{tier_name}** ({messages} messages) and unlocked {role_mention}!",
    "tiers": [],
}

# Per-user cooldown tracking so rapid message bursts only count once per window. Lost on restart, which is fine.
_last_counted_at: dict[str, datetime] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_tier(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    try:
        threshold = max(1, int(raw.get("threshold", 0) or 0))
    except (TypeError, ValueError):
        return None
    role_id = str(raw.get("role_id") or "").strip()
    if not role_id:
        return None
    name = str(raw.get("name") or "").strip() or f"Level {threshold}"
    tier_id = str(raw.get("id") or "").strip() or uuid.uuid4().hex[:12]
    return {"id": tier_id, "name": name, "threshold": threshold, "role_id": role_id}


def _guild_cfg(guild_id: int) -> dict:
    root = load_json(LEVELING_CONFIG_FILE, {})
    if not isinstance(root, dict):
        root = {}
    cfg = root.get(str(guild_id), {})
    if not isinstance(cfg, dict):
        cfg = {}
    merged = {**DEFAULT_CONFIG, **cfg}
    merged["enabled"] = bool(merged.get("enabled", True))
    merged["cooldown_seconds"] = max(0, int(merged.get("cooldown_seconds", 5) or 0))
    merged["announce_level_up"] = bool(merged.get("announce_level_up", True))
    announce_ch = merged.get("announce_channel_id")
    merged["announce_channel_id"] = str(announce_ch) if announce_ch else None
    excluded = merged.get("excluded_channel_ids", [])
    merged["excluded_channel_ids"] = [str(c) for c in excluded] if isinstance(excluded, list) else []
    tiers_raw = merged.get("tiers", [])
    tiers = [_normalize_tier(t) for t in tiers_raw] if isinstance(tiers_raw, list) else []
    merged["tiers"] = sorted([t for t in tiers if t], key=lambda t: t["threshold"])
    return merged


def _load_stats() -> dict:
    data = load_json(LEVELING_STATS_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_stats(data: dict) -> None:
    save_json(LEVELING_STATS_FILE, data)


def _channel_and_parents(channel: discord.abc.GuildChannel) -> set[str]:
    ids = {str(channel.id)}
    parent = getattr(channel, "parent", None)
    if parent is not None:
        ids.add(str(parent.id))
    category_id = getattr(channel, "category_id", None)
    if category_id:
        ids.add(str(category_id))
    return ids


async def _announce_level_up(
    guild: discord.Guild,
    member: discord.Member,
    message_channel: discord.abc.Messageable,
    tier: dict,
    role: discord.Role,
    cfg: dict,
    message_count: int,
) -> None:
    text = str(cfg.get("level_up_message") or DEFAULT_CONFIG["level_up_message"])
    text = (
        text.replace("{mention}", member.mention)
        .replace("{user}", member.display_name)
        .replace("{tier_name}", tier["name"])
        .replace("{messages}", str(message_count))
        .replace("{role_mention}", role.mention)
    )

    target = message_channel
    announce_channel_id = cfg.get("announce_channel_id")
    if announce_channel_id:
        try:
            channel = guild.get_channel(int(announce_channel_id))
        except (TypeError, ValueError):
            channel = None
        if channel is not None:
            target = channel

    try:
        await target.send(text)
    except discord.HTTPException:
        pass


async def _grant_tiers(
    guild: discord.Guild,
    member: discord.Member,
    message_channel: discord.abc.Messageable,
    cfg: dict,
    row: dict,
    message_count: int,
) -> None:
    granted = row.get("granted_tier_ids", [])
    if not isinstance(granted, list):
        granted = []

    newly_granted = False
    for tier in cfg.get("tiers", []):
        if message_count < tier["threshold"]:
            continue
        if tier["id"] in granted:
            continue

        role = guild.get_role(int(tier["role_id"]))
        if role is None:
            granted.append(tier["id"])
            newly_granted = True
            continue

        try:
            await member.add_roles(role, reason=f"Leveling: reached {tier['name']} ({tier['threshold']} messages)")
        except discord.HTTPException:
            continue

        granted.append(tier["id"])
        newly_granted = True

        if cfg.get("announce_level_up", True):
            await _announce_level_up(guild, member, message_channel, tier, role, cfg, message_count)

    if newly_granted:
        row["granted_tier_ids"] = granted


async def _track_message(message: discord.Message) -> None:
    if _bot is None or not message.guild or message.author.bot:
        return

    content = (message.content or "").strip()
    if not content and not message.attachments:
        return

    guild = message.guild
    channel = message.channel
    cfg = _guild_cfg(guild.id)
    if not cfg.get("enabled", True):
        return

    relevant_channel_ids = _channel_and_parents(channel)
    if relevant_channel_ids & set(cfg.get("excluded_channel_ids", [])):
        return

    user_id = message.author.id
    now_utc = _now()
    cooldown = int(cfg.get("cooldown_seconds", 5))
    dedupe_key = f"{guild.id}:{user_id}"
    if cooldown > 0:
        last = _last_counted_at.get(dedupe_key)
        if last and (now_utc - last).total_seconds() < cooldown:
            return
    _last_counted_at[dedupe_key] = now_utc

    all_stats = _load_stats()
    guild_stats = all_stats.get(str(guild.id), {})
    if not isinstance(guild_stats, dict):
        guild_stats = {}
    row = guild_stats.get(str(user_id), {})
    if not isinstance(row, dict):
        row = {}

    message_count = int(row.get("message_count", 0)) + 1
    row["message_count"] = message_count
    row["last_message_at"] = now_utc.isoformat()

    member = message.author if isinstance(message.author, discord.Member) else guild.get_member(user_id)
    if member is not None and cfg.get("tiers"):
        await _grant_tiers(guild, member, channel, cfg, row, message_count)

    guild_stats[str(user_id)] = row
    all_stats[str(guild.id)] = guild_stats
    _save_stats(all_stats)


def get_leveling_leaderboard(guild_id: int, limit: int = 15) -> list[dict]:
    stats = _load_stats().get(str(guild_id), {})
    if not isinstance(stats, dict):
        return []

    guild = _bot.get_guild(guild_id) if _bot else None
    cfg = _guild_cfg(guild_id)
    tiers = cfg.get("tiers", [])

    rows: list[dict] = []
    for uid, row in stats.items():
        if not isinstance(row, dict):
            continue
        try:
            user_id = int(uid)
        except ValueError:
            continue

        user_name = None
        if guild:
            member = guild.get_member(user_id)
            if member:
                user_name = member.display_name or member.name
        if not user_name and _bot:
            user = _bot.get_user(user_id)
            if user:
                user_name = user.display_name or user.name

        message_count = int(row.get("message_count", 0) or 0)
        current_tier = None
        for tier in tiers:
            if message_count >= tier["threshold"]:
                current_tier = tier["name"]

        rows.append(
            {
                "user_id": str(user_id),
                "user_name": user_name or "Unknown User",
                "message_count": message_count,
                "current_tier": current_tier,
                "last_message_at": row.get("last_message_at"),
            }
        )
    rows.sort(key=lambda r: r["message_count"], reverse=True)
    return rows[: max(1, min(50, int(limit or 15)))]


def _tier_progress(guild_id: int, user_id: int) -> dict:
    cfg = _guild_cfg(guild_id)
    stats = _load_stats().get(str(guild_id), {})
    row = stats.get(str(user_id)) if isinstance(stats, dict) else None
    message_count = int(row.get("message_count", 0)) if isinstance(row, dict) else 0

    tiers = cfg.get("tiers", [])
    current_tier = None
    next_tier = None
    for tier in tiers:
        if message_count >= tier["threshold"]:
            current_tier = tier
        elif next_tier is None:
            next_tier = tier

    return {
        "message_count": message_count,
        "current_tier": current_tier,
        "next_tier": next_tier,
    }


# ----- Slash commands: rank, leaderboard, tiers overview -----
level_group = app_commands.Group(name="level", description="Leveling system commands")


@level_group.command(name="rank", description="Show your (or another member's) leveling progress")
@app_commands.describe(member="Member to check (defaults to you)")
async def level_rank(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    if interaction.guild is None:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return

    target = member or interaction.user
    progress = _tier_progress(interaction.guild.id, target.id)

    embed = discord.Embed(
        title=f"📈 Leveling Progress — {target.display_name}",
        color=discord.Color.blue(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="💬 Messages", value=f"`{progress['message_count']}`", inline=True)

    current = progress.get("current_tier")
    embed.add_field(name="🏅 Current Tier", value=current["name"] if current else "None yet", inline=True)

    next_tier = progress.get("next_tier")
    if next_tier:
        remaining = max(0, next_tier["threshold"] - progress["message_count"])
        embed.add_field(
            name="🎯 Next Tier",
            value=f"{next_tier['name']} — `{remaining}` messages to go",
            inline=False,
        )
    else:
        embed.add_field(name="🎯 Next Tier", value="Max tier reached (or none configured)", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=False)


@level_group.command(name="leaderboard", description="Show the leveling leaderboard for this server")
@app_commands.describe(limit="How many rows to show (max 50)")
async def level_leaderboard(interaction: discord.Interaction, limit: int = 15):
    if interaction.guild is None:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)
    limit = max(1, min(50, int(limit or 15)))
    rows = get_leveling_leaderboard(interaction.guild.id, limit=limit)
    if not rows:
        await interaction.followup.send("No leveling stats available for this server.", ephemeral=True)
        return

    desc_lines = []
    for idx, r in enumerate(rows, start=1):
        tier_note = f" • {r['current_tier']}" if r.get("current_tier") else ""
        desc_lines.append(f"**#{idx}** • {r['user_name']} — `{r['message_count']}` messages{tier_note}")

    embed = discord.Embed(
        title=f"Leveling Leaderboard — {interaction.guild.name}",
        description="\n".join(desc_lines),
        color=discord.Color.blue(),
    )
    if interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    await interaction.followup.send(embed=embed)


@level_group.command(name="tiers", description="List the configured leveling tiers and their roles")
async def level_tiers(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return

    cfg = _guild_cfg(interaction.guild.id)
    tiers = cfg.get("tiers", [])
    if not tiers:
        await interaction.response.send_message("No leveling tiers configured yet.", ephemeral=True)
        return

    lines = []
    for tier in tiers:
        role = interaction.guild.get_role(int(tier["role_id"]))
        role_text = role.mention if role else f"`{tier['role_id']}` (role not found)"
        lines.append(f"**{tier['name']}** — `{tier['threshold']}` messages → {role_text}")

    embed = discord.Embed(title="Leveling Tiers", description="\n".join(lines), color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)


def register(bot: discord.ext.commands.Bot):
    global _bot
    _bot = bot

    async def _leveling_on_message(message: discord.Message):
        await _track_message(message)

    # Use listener to avoid replacing other on_message handlers.
    bot.add_listener(_leveling_on_message, "on_message")
    try:
        bot.tree.add_command(level_group)
    except Exception:
        # If command already added or tree not ready, ignore
        pass
