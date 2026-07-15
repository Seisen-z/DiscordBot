"""
modules/member_counter.py
Realtime member counter – config, core logic, background loop, slash commands.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import tasks

from modules.utils import _as_int, load_json, save_json

# ── Bot reference ─────────────────────────────────────────────────────────────
_bot: discord.ext.commands.Bot | None = None

# Per-guild cooldown: tracks when we last successfully edited the counter channel.
# Discord allows ~2 channel edits per 10 minutes; we enforce 5 min minimum spacing.
_last_counter_update: dict[str, float] = {}
_COUNTER_COOLDOWN_SECONDS = 300  # 5 minutes

MEMBER_COUNTER_FILE = "member_counter_configs"
MEMBER_COUNTER_DEFAULT_PREFIX = "Members: "


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_member_counter_type(value) -> str:
    text = str(value or "voice").strip().lower()
    return "text" if text == "text" else "voice"


def _normalize_member_counter_prefix(value) -> str:
    prefix = str(value or MEMBER_COUNTER_DEFAULT_PREFIX).strip()
    if not prefix:
        prefix = MEMBER_COUNTER_DEFAULT_PREFIX.strip()
    if not prefix.endswith(" "):
        prefix += " "
    return prefix[:90]


def _normalize_member_counter_entry(raw) -> dict:
    source = raw if isinstance(raw, dict) else {}
    channel_id = _as_int(source.get("channel_id"))
    category_id = _as_int(source.get("category_id"))
    enabled = bool(source.get("enabled", False)) and bool(channel_id)
    return {
        "enabled": enabled,
        "channel_id": channel_id,
        "channel_type": _normalize_member_counter_type(source.get("channel_type")),
        "category_id": category_id,
        "prefix": _normalize_member_counter_prefix(source.get("prefix")),
        "include_bots": bool(source.get("include_bots", False)),
        "last_count": max(0, _as_int(source.get("last_count")) or 0),
        "last_updated": str(source.get("last_updated") or "") or None,
        "created_at": str(source.get("created_at") or "") or None,
        "created_by": _as_int(source.get("created_by")),
    }


def _serialize_member_counter_entry(entry: dict) -> dict:
    normalized = _normalize_member_counter_entry(entry)

    def to_str_id(v) -> str | None:
        p = _as_int(v)
        return str(p) if p else None

    return {
        "enabled": bool(normalized.get("enabled", False)),
        "channel_id": to_str_id(normalized.get("channel_id")),
        "channel_type": _normalize_member_counter_type(normalized.get("channel_type")),
        "category_id": to_str_id(normalized.get("category_id")),
        "prefix": _normalize_member_counter_prefix(normalized.get("prefix")),
        "include_bots": bool(normalized.get("include_bots", False)),
        "last_count": max(0, _as_int(normalized.get("last_count")) or 0),
        "last_updated": str(normalized.get("last_updated") or "") or None,
        "created_at": str(normalized.get("created_at") or "") or None,
        "created_by": to_str_id(normalized.get("created_by")),
    }


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_member_counter_config() -> dict:
    raw = load_json(MEMBER_COUNTER_FILE, {})
    if not isinstance(raw, dict):
        return {}
    return {str(gid): _normalize_member_counter_entry(entry) for gid, entry in raw.items()}


def save_member_counter_config(data: dict):
    payload = {str(gid): _normalize_member_counter_entry(entry) for gid, entry in (data or {}).items()}
    save_json(MEMBER_COUNTER_FILE, payload)


def get_member_counter_entry(guild_id: int) -> dict:
    return _normalize_member_counter_entry(load_member_counter_config().get(str(guild_id), {}))


# ── Core logic ────────────────────────────────────────────────────────────────

def _member_counter_count(guild: discord.Guild, include_bots: bool) -> int:
    if include_bots:
        return max(0, int(guild.member_count or len(guild.members) or 0))
    if guild.members:
        return max(0, sum(1 for m in guild.members if not m.bot))
    fallback = int(guild.member_count or 0)
    if guild.me is not None:
        fallback = max(0, fallback - 1)
    return fallback


def _member_counter_channel_name(prefix: str, member_count: int) -> str:
    return f"{_normalize_member_counter_prefix(prefix)}{max(0, int(member_count))}"[:100]


async def _resolve_member_counter_channel(guild: discord.Guild, channel_id: int | None):
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            channel = None
    return channel


async def _update_member_counter_channel(
    guild: discord.Guild, *, force: bool = False, allow_disabled: bool = False
) -> tuple[bool, str | None, dict]:
    config_data = load_member_counter_config()
    guild_key = str(guild.id)
    entry = _normalize_member_counter_entry(config_data.get(guild_key, {}))

    if not entry.get("enabled") and not allow_disabled:
        return False, "Member counter is disabled.", entry

    if allow_disabled and not _as_int(entry.get("channel_id")):
        return False, "No member counter channel configured.", entry

    channel_id = _as_int(entry.get("channel_id"))
    channel = await _resolve_member_counter_channel(guild, channel_id)
    if channel is None:
        entry["enabled"] = False
        config_data[guild_key] = entry
        save_member_counter_config(config_data)
        return False, "Counter channel no longer exists. Counter was disabled.", entry

    if not hasattr(channel, "edit"):
        return False, "Channel type does not support renaming.", entry

    try:
        perms = channel.permissions_for(guild.me) if guild.me else None
    except Exception:
        perms = None
    if perms is not None and not perms.manage_channels:
        return False, "Bot is missing Manage Channels permission.", entry

    current_count = _member_counter_count(guild, bool(entry.get("include_bots")))
    target_name = _member_counter_channel_name(entry.get("prefix"), current_count)
    current_name = str(getattr(channel, "name", "") or "")

    changed = False
    if force or current_name != target_name:
        await channel.edit(name=target_name, reason="Realtime member counter update")
        changed = True

    previous_count = _as_int(entry.get("last_count")) or 0
    if changed or previous_count != current_count:
        entry["last_count"] = current_count
        entry["last_updated"] = datetime.now(timezone.utc).isoformat()
        config_data[guild_key] = entry
        save_member_counter_config(config_data)

    return True, None, entry


async def create_member_counter_channel(
    guild: discord.Guild,
    *,
    channel_type: str,
    prefix: str,
    include_bots: bool,
    category_id: int | None,
    created_by_user_id: int | None,
) -> tuple[bool, str, dict | None]:
    normalized_type = _normalize_member_counter_type(channel_type)
    normalized_prefix = _normalize_member_counter_prefix(prefix)

    category = None
    if category_id:
        category = guild.get_channel(category_id)
        if category is None:
            try:
                category = await guild.fetch_channel(category_id)
            except Exception:
                category = None
        if category is not None and not isinstance(category, discord.CategoryChannel):
            return False, "Selected category is not a valid category channel.", None

    count_value = _member_counter_count(guild, include_bots)
    initial_name = _member_counter_channel_name(normalized_prefix, count_value)

    try:
        if normalized_type == "text":
            created_channel = await guild.create_text_channel(
                name=initial_name,
                category=category,
                reason="Create realtime member counter channel",
            )
        else:
            created_channel = await guild.create_voice_channel(
                name=initial_name,
                category=category,
                reason="Create realtime member counter channel",
            )
    except Exception as e:
        return False, f"Failed to create counter channel: {e}", None

    config_data = load_member_counter_config()
    entry = _normalize_member_counter_entry({
        "enabled": True,
        "channel_id": created_channel.id,
        "channel_type": normalized_type,
        "category_id": category.id if category else None,
        "prefix": normalized_prefix,
        "include_bots": include_bots,
        "last_count": count_value,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": created_by_user_id,
    })
    config_data[str(guild.id)] = entry
    save_member_counter_config(config_data)
    return True, "Member counter channel created.", entry


async def configure_member_counter_channel(
    guild: discord.Guild,
    *,
    channel_id: int,
    prefix: str,
    include_bots: bool,
    enabled: bool,
) -> tuple[bool, str, dict | None]:
    channel = await _resolve_member_counter_channel(guild, channel_id)
    if channel is None:
        return False, "Channel could not be found.", None

    inferred_type = "voice" if isinstance(channel, discord.VoiceChannel) else "text"
    config_data = load_member_counter_config()
    guild_key = str(guild.id)
    existing = _normalize_member_counter_entry(config_data.get(guild_key, {}))
    existing.update({
        "enabled": bool(enabled),
        "channel_id": channel.id,
        "channel_type": inferred_type,
        "prefix": _normalize_member_counter_prefix(prefix),
        "include_bots": bool(include_bots),
    })
    config_data[guild_key] = existing
    save_member_counter_config(config_data)

    if existing.get("enabled"):
        try:
            ok, err, updated = await _update_member_counter_channel(guild, force=True)
            if not ok:
                return False, str(err or "Failed to sync member counter."), updated
            return True, "Member counter configured and synced.", updated
        except Exception as sync_err:
            return False, f"Configured but failed to sync now: {sync_err}", existing

    return True, "Member counter configuration saved (disabled).", existing


async def sync_member_counter_now(guild: discord.Guild) -> tuple[bool, str, dict | None]:
    was_enabled = bool(get_member_counter_entry(guild.id).get("enabled"))
    ok, err, entry = await _update_member_counter_channel(guild, force=True, allow_disabled=True)
    if not ok:
        return False, str(err or "Failed to sync member counter."), entry
    if not was_enabled:
        return True, "Member counter synced once (counter remains disabled).", entry
    return True, "Member counter synced.", entry


# ── Background loop ───────────────────────────────────────────────────────────

@tasks.loop(minutes=5)
async def member_counter_update_check():
    try:
        now = datetime.now(timezone.utc).timestamp()
        config_data = await asyncio.to_thread(load_member_counter_config)
        for guild_id, raw_entry in config_data.items():
            gid = _as_int(guild_id)
            if not gid:
                continue
            entry = _normalize_member_counter_entry(raw_entry)
            if not entry.get("enabled"):
                continue
            # Enforce per-guild cooldown to avoid hitting Discord's channel-edit rate limit.
            last_update = _last_counter_update.get(guild_id, 0)
            if now - last_update < _COUNTER_COOLDOWN_SECONDS:
                continue
            guild = _bot.get_guild(gid)
            if guild is None:
                continue
            try:
                ok, _err, _entry = await _update_member_counter_channel(guild, force=False)
                if ok:
                    _last_counter_update[guild_id] = now
            except Exception as e:
                print(f"[MemberCounter] Update failed for guild {gid}: {e}")
    except Exception as e:
        print(f"[MemberCounter] Loop error: {e}")


@member_counter_update_check.before_loop
async def _before_member_counter_update_check():
    await _bot.wait_until_ready()


# Backward-compat alias for older main.py imports.
member_counter_update_loop = member_counter_update_check


# ── Slash commands ────────────────────────────────────────────────────────────

membercounter_group = app_commands.Group(name="membercounter", description="Realtime member counter commands")


@membercounter_group.command(name="create", description="Create a realtime member counter channel")
@app_commands.describe(
    channel_type="Choose whether to create a voice or text counter channel",
    prefix="Channel name prefix (example: Members: )",
    include_bots="Include bots in the counter value",
    category="Optional category for the new counter channel",
)
@app_commands.choices(channel_type=[
    app_commands.Choice(name="Voice Channel", value="voice"),
    app_commands.Choice(name="Text Channel", value="text"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def membercounter_create(
    interaction: discord.Interaction,
    channel_type: app_commands.Choice[str] = None,
    prefix: str = MEMBER_COUNTER_DEFAULT_PREFIX,
    include_bots: bool = False,
    category: discord.CategoryChannel = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Server only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    success, message, entry = await create_member_counter_channel(
        interaction.guild,
        channel_type=channel_type.value if channel_type else "voice",
        prefix=prefix,
        include_bots=include_bots,
        category_id=category.id if category else None,
        created_by_user_id=interaction.user.id,
    )

    if not success or entry is None:
        await interaction.followup.send(f"❌ {message}", ephemeral=True)
        return

    channel_id = _as_int(entry.get("channel_id"))
    channel_mention = f"<#{channel_id}>" if channel_id else "the new channel"
    await interaction.followup.send(
        f"✅ Member counter channel created: {channel_mention}\n"
        f"Prefix: `{entry.get('prefix')}` | Bots included: `{entry.get('include_bots')}`",
        ephemeral=True,
    )


@membercounter_group.command(name="configure", description="Configure an existing channel as member counter")
@app_commands.describe(
    channel="An existing channel to use as the counter",
    prefix="Channel name prefix",
    include_bots="Include bots in the count",
    enabled="Enable or disable the counter",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def membercounter_configure(
    interaction: discord.Interaction,
    channel: discord.abc.GuildChannel,
    prefix: str = MEMBER_COUNTER_DEFAULT_PREFIX,
    include_bots: bool = False,
    enabled: bool = True,
):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Server only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    success, message, entry = await configure_member_counter_channel(
        interaction.guild,
        channel_id=channel.id,
        prefix=prefix,
        include_bots=include_bots,
        enabled=enabled,
    )
    if not success:
        await interaction.followup.send(f"❌ {message}", ephemeral=True)
    else:
        await interaction.followup.send(f"✅ {message}", ephemeral=True)


@membercounter_group.command(name="remove", description="Remove the member counter for this server")
@app_commands.checks.has_permissions(manage_guild=True)
async def membercounter_remove(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Server only.", ephemeral=True)
        return
    config_data = load_member_counter_config()
    guild_key = str(interaction.guild.id)
    if guild_key not in config_data:
        await interaction.response.send_message("❌ No member counter configured.", ephemeral=True)
        return
    del config_data[guild_key]
    save_member_counter_config(config_data)
    await interaction.response.send_message("✅ Member counter removed.", ephemeral=True)


@membercounter_group.command(name="status", description="Show current member counter configuration")
@app_commands.checks.has_permissions(manage_guild=True)
async def membercounter_status(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Server only.", ephemeral=True)
        return
    entry = get_member_counter_entry(interaction.guild.id)
    if not entry.get("channel_id"):
        await interaction.response.send_message("❌ No member counter configured. Use `/membercounter create`.", ephemeral=True)
        return
    channel_id = _as_int(entry.get("channel_id"))
    embed = discord.Embed(title="📊 Member Counter Status", color=discord.Color.blurple())
    embed.add_field(name="Status", value="✅ Enabled" if entry.get("enabled") else "❌ Disabled", inline=True)
    embed.add_field(name="Channel", value=f"<#{channel_id}>" if channel_id else "Unknown", inline=True)
    embed.add_field(name="Type", value=entry.get("channel_type", "voice").capitalize(), inline=True)
    embed.add_field(name="Prefix", value=f"`{entry.get('prefix', MEMBER_COUNTER_DEFAULT_PREFIX)}`", inline=True)
    embed.add_field(name="Include Bots", value="Yes" if entry.get("include_bots") else "No", inline=True)
    embed.add_field(name="Last Count", value=str(entry.get("last_count", 0)), inline=True)
    if entry.get("last_updated"):
        embed.add_field(name="Last Updated", value=entry["last_updated"], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: discord.ext.commands.Bot):
    global _bot
    _bot = bot
    bot.tree.add_command(membercounter_group)

