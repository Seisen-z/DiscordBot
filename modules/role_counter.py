"""
modules/role_counter.py
Role count panels — posts an embed listing multiple roles and their member counts,
auto-edits the message whenever roles are assigned or removed.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import tasks

from modules.utils import _as_int, load_json, save_json

_bot: discord.ext.commands.Bot | None = None

ROLE_PANEL_FILE = "role_panel_configs"

# Per-panel edit cooldown so we don't hammer Discord on rapid role changes.
_last_update: dict[str, float] = {}
_COOLDOWN_SECONDS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Config helpers ─────────────────────────────────────────────────────────────

def _normalize_panel(raw: dict) -> dict:
    return {
        "id": str(raw.get("id") or uuid.uuid4()),
        "channel_id": str(raw.get("channel_id") or ""),
        "message_id": str(raw.get("message_id") or "") or None,
        "title": str(raw.get("title") or "Role Members"),
        "role_ids": [str(r) for r in (raw.get("role_ids") or []) if r],
        "enabled": bool(raw.get("enabled", True)),
        "last_updated": str(raw.get("last_updated") or "") or None,
    }


def load_guild_panels(guild_id: int) -> list[dict]:
    root = load_json(ROLE_PANEL_FILE, {})
    raw = root.get(str(guild_id), [])
    if not isinstance(raw, list):
        return []
    return [_normalize_panel(e) for e in raw if isinstance(e, dict)]


def save_guild_panels(guild_id: int, panels: list[dict]) -> None:
    root = load_json(ROLE_PANEL_FILE, {})
    if not isinstance(root, dict):
        root = {}
    root[str(guild_id)] = [_normalize_panel(p) for p in panels]
    save_json(ROLE_PANEL_FILE, root)


# ── Embed builder ──────────────────────────────────────────────────────────────

def _build_embed(guild: discord.Guild, panel: dict) -> discord.Embed:
    embed = discord.Embed(
        title=panel.get("title") or "Role Members",
        color=discord.Color.blurple(),
        timestamp=_now(),
    )
    role_ids = panel.get("role_ids") or []
    if not role_ids:
        embed.description = "_No roles configured._"
        return embed

    rows: list[tuple[int, str]] = []
    for rid in role_ids:
        role = guild.get_role(_as_int(rid))
        count = sum(1 for m in role.members if not m.bot) if role else 0
        mention = f"<@&{rid}>"
        rows.append((count, mention))

    FIGURE = " "  # same width as a digit in Discord's font
    max_digits = max((len(str(c)) for c, _ in rows), default=1)
    lines = [
        f"{FIGURE * (max_digits - len(str(c)))}{c}  =  {mention}"
        for c, mention in rows
    ]

    embed.description = "\n".join(lines)
    embed.set_footer(text="Auto-updates on role changes")
    return embed


# ── Core update ────────────────────────────────────────────────────────────────

async def _update_panel(guild: discord.Guild, panel: dict, *, force: bool = False) -> bool:
    if not panel.get("enabled"):
        return False

    panel_id = panel["id"]
    channel_id = _as_int(panel.get("channel_id"))
    message_id = _as_int(panel.get("message_id"))
    if not channel_id or not message_id:
        return False

    # Short cooldown to debounce rapid role changes
    if not force:
        last = _last_update.get(panel_id, 0.0)
        if (_now().timestamp() - last) < _COOLDOWN_SECONDS:
            return False

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            return False

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return False

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        # Message was deleted — clear message_id so dashboard shows it's gone
        panels = load_guild_panels(guild.id)
        for p in panels:
            if p["id"] == panel_id:
                p["message_id"] = None
                break
        save_guild_panels(guild.id, panels)
        return False
    except Exception:
        return False

    embed = _build_embed(guild, panel)
    try:
        await message.edit(embed=embed)
    except discord.HTTPException:
        return False

    _last_update[panel_id] = _now().timestamp()

    panels = load_guild_panels(guild.id)
    for p in panels:
        if p["id"] == panel_id:
            p["last_updated"] = _now().isoformat()
            break
    save_guild_panels(guild.id, panels)
    return True


async def _update_panels_for_roles(guild: discord.Guild, changed_role_ids: set[int]) -> None:
    panels = load_guild_panels(guild.id)
    changed_strs = {str(r) for r in changed_role_ids}
    for panel in panels:
        if not panel.get("enabled"):
            continue
        if any(r in changed_strs for r in (panel.get("role_ids") or [])):
            asyncio.create_task(_update_panel(guild, panel, force=False))


# ── Post / repost panel ────────────────────────────────────────────────────────

async def post_panel(guild_id: int, panel_id: str) -> tuple[bool, str]:
    if _bot is None:
        return False, "Bot not ready."
    guild = _bot.get_guild(guild_id)
    if guild is None:
        return False, "Guild not found."

    panels = load_guild_panels(guild_id)
    panel = next((p for p in panels if p["id"] == panel_id), None)
    if panel is None:
        return False, "Panel not found."

    channel_id = _as_int(panel.get("channel_id"))
    if not channel_id:
        return False, "No channel configured."

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            return False, "Channel not found."

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return False, "Channel must be a text channel."

    embed = _build_embed(guild, panel)
    try:
        msg = await channel.send(embed=embed)
    except discord.HTTPException as e:
        return False, f"Failed to send panel: {e}"

    for p in panels:
        if p["id"] == panel_id:
            p["message_id"] = str(msg.id)
            p["last_updated"] = _now().isoformat()
            break
    save_guild_panels(guild_id, panels)
    return True, str(msg.id)


async def sync_panel(guild_id: int, panel_id: str) -> tuple[bool, str]:
    if _bot is None:
        return False, "Bot not ready."
    guild = _bot.get_guild(guild_id)
    if guild is None:
        return False, "Guild not found."
    panels = load_guild_panels(guild_id)
    panel = next((p for p in panels if p["id"] == panel_id), None)
    if panel is None:
        return False, "Panel not found."
    ok = await _update_panel(guild, panel, force=True)
    return ok, "Updated." if ok else "Panel message not found or not posted yet."


# ── Background loop ────────────────────────────────────────────────────────────

@tasks.loop(minutes=5)
async def role_counter_update_loop():
    if _bot is None:
        return
    try:
        root = load_json(ROLE_PANEL_FILE, {})
        if not isinstance(root, dict):
            return
        for guild_id_str, raw_list in root.items():
            gid = _as_int(guild_id_str)
            if not gid:
                continue
            guild = _bot.get_guild(gid)
            if guild is None:
                continue
            if not isinstance(raw_list, list):
                continue
            for raw in raw_list:
                if not isinstance(raw, dict):
                    continue
                panel = _normalize_panel(raw)
                if not panel.get("enabled") or not panel.get("message_id"):
                    continue
                try:
                    await _update_panel(guild, panel, force=True)
                except Exception as e:
                    print(f"[RoleCounter] Update failed guild={gid}: {e}")
    except Exception as e:
        print(f"[RoleCounter] Loop error: {e}")


@role_counter_update_loop.before_loop
async def _before_role_counter_loop():
    if _bot is not None:
        await _bot.wait_until_ready()


# ── Event listener ─────────────────────────────────────────────────────────────

async def _on_member_update(before: discord.Member, after: discord.Member) -> None:
    if before.guild is None:
        return
    before_roles = {r.id for r in before.roles}
    after_roles = {r.id for r in after.roles}
    changed = before_roles.symmetric_difference(after_roles)
    if changed:
        await _update_panels_for_roles(before.guild, changed)


# ── Registration ───────────────────────────────────────────────────────────────

def register(bot: discord.ext.commands.Bot) -> None:
    global _bot
    _bot = bot
    bot.add_listener(_on_member_update, "on_member_update")
