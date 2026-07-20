"""
modules/channel_access.py
Role -> channel access mappings. Each mapping is applied as a channel permission
overwrite for the mapped role, so anyone holding that role automatically sees
(and optionally can post in) the mapped channel — enforced natively by Discord,
so no per-member bookkeeping or listeners are needed once a mapping is synced.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

import discord

_bot: discord.ext.commands.Bot | None = None


def _normalize_mapping(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    role_id = str(raw.get("role_id") or "").strip()
    channel_id = str(raw.get("channel_id") or "").strip()
    if not role_id or not channel_id:
        return None
    mapping_id = str(raw.get("id") or "").strip() or uuid.uuid4().hex[:12]
    return {
        "id": mapping_id,
        "role_id": role_id,
        "channel_id": channel_id,
        "view_channel": bool(raw.get("view_channel", True)),
        "send_messages": bool(raw.get("send_messages", True)),
    }


async def sync_mappings_for_guild(guild: discord.Guild, mappings: list) -> tuple[int, int]:
    """Apply each role->channel mapping as a permission overwrite on that channel for that role."""
    applied = 0
    failed = 0
    for raw in mappings if isinstance(mappings, list) else []:
        mapping = _normalize_mapping(raw)
        if not mapping:
            failed += 1
            continue

        try:
            role = guild.get_role(int(mapping["role_id"]))
            channel = guild.get_channel(int(mapping["channel_id"]))
        except (TypeError, ValueError):
            role = None
            channel = None

        if role is None or channel is None:
            failed += 1
            continue

        overwrite = channel.overwrites_for(role)
        overwrite.view_channel = mapping["view_channel"]
        overwrite.send_messages = mapping["send_messages"]

        try:
            await channel.set_permissions(role, overwrite=overwrite, reason="Channel access mapping sync")
            applied += 1
        except discord.HTTPException:
            failed += 1

    return applied, failed


def register(bot: discord.ext.commands.Bot):
    global _bot
    _bot = bot
