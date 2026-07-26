"""
modules/auto_post.py
Auto-Post background task that periodically posts configured messages/embeds
to target channels and deletes the previous post.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import discord
from discord.ext import commands, tasks
from typing import Optional, Dict, Any

from modules.utils import load_json, save_json, _as_int
from modules.dashboard_handlers import resolve_local_asset, resolve_trigger_channel

_bot: commands.Bot | None = None
AUTO_POST_DB = "auto_posts"

async def execute_auto_post_send(guild: discord.Guild, data: dict) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """
    Sends or re-posts an auto-post message to the target channel.
    Deletes the previous message if last_message_id is provided.
    Returns (success, message_id, timestamp_iso, error_message).
    """
    channel_id = _as_int(data.get("channel_id"))
    if not channel_id:
        return False, None, None, "No target channel configured."

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            channel = None

    if not channel or not hasattr(channel, "send"):
        return False, None, None, f"Channel {channel_id} not found or not text-based."

    # Delete previous message if it exists
    old_msg_id = data.get("last_message_id")
    if old_msg_id and str(old_msg_id).isdigit():
        try:
            old_msg = await channel.fetch_message(int(old_msg_id))
            await old_msg.delete()
        except Exception as exc:
            print(f"[AutoPost] Could not delete old message {old_msg_id} in #{channel.name}: {exc}")

    # Build content, embeds, attachments, and buttons
    title = data.get("title", "")
    desc = data.get("description", "")
    msg_content = data.get("content", "")

    files = []
    thumb = resolve_local_asset(data.get("thumbnail_url"), files)
    image = resolve_local_asset(data.get("image_url"), files)
    images = [resolve_local_asset(img, files) for img in data.get("images", [])]
    footer = data.get("footer")
    role_id = data.get("ping_role_id")

    embeds = []
    main_embed = discord.Embed(title=title, description=desc, color=discord.Color.from_str("#3b82f6"))
    if thumb:
        main_embed.set_thumbnail(url=thumb)
    if image:
        main_embed.set_image(url=image)
    if footer:
        main_embed.set_footer(text=footer)

    if title or desc or thumb or image or footer:
        embeds.append(main_embed)

    parts = []
    if role_id:
        parts.append(f"<@&{role_id}>")
    if msg_content:
        parts.append(msg_content)

    for img in images:
        if img and img.strip() and not img.startswith("attachment://"):
            parts.append(img.strip())

    final_content = "\n".join(parts)

    if not final_content and not embeds and not files:
        return False, None, None, "Cannot post empty message."

    view = None
    buttons_data = data.get("buttons") or []
    valid_buttons = [b for b in buttons_data if isinstance(b, dict) and b.get("label") and b.get("url")]
    if valid_buttons:
        view = discord.ui.View(timeout=None)
        for b in valid_buttons[:5]:
            url = str(b.get("url", "")).strip()
            if not (url.startswith("http://") or url.startswith("https://") or url.startswith("discord://")):
                url = f"https://{url}"
            view.add_item(discord.ui.Button(label=str(b.get("label"))[:80], url=url))

    try:
        new_msg = await channel.send(
            content=final_content or None,
            embeds=embeds or None,
            view=view,
            files=files or None,
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        return True, str(new_msg.id), now_iso, None
    except Exception as exc:
        err_msg = f"Failed to send auto-post message: {exc}"
        print(f"[AutoPost] {err_msg}")
        return False, None, None, err_msg


@tasks.loop(seconds=30)
async def auto_post_check_loop():
    """Background task loop checking active auto-posts across all guilds."""
    if _bot is None or not _bot.is_ready():
        return

    try:
        data = load_json(AUTO_POST_DB, {})
        if not isinstance(data, dict):
            return

        modified = False
        now = datetime.now(timezone.utc)

        for guild_id_str, guild_posts in data.items():
            if not isinstance(guild_posts, dict):
                continue

            try:
                guild_id = int(guild_id_str)
                guild = _bot.get_guild(guild_id)
            except ValueError:
                guild = None

            if not guild:
                continue

            for post_key, post_cfg in guild_posts.items():
                if not isinstance(post_cfg, dict):
                    continue

                if not post_cfg.get("enabled", False):
                    continue

                channel_id = post_cfg.get("channel_id")
                if not channel_id:
                    continue

                # Calculate interval in seconds
                interval_minutes = post_cfg.get("interval_minutes")
                if interval_minutes is None:
                    interval_hours = post_cfg.get("interval_hours", 1)
                    interval_minutes = float(interval_hours) * 60.0
                else:
                    interval_minutes = float(interval_minutes)

                if interval_minutes <= 0:
                    interval_minutes = 60.0

                interval_seconds = interval_minutes * 60.0

                # Check last posted time
                last_posted_at_raw = post_cfg.get("last_posted_at")
                should_post = False

                if not last_posted_at_raw:
                    should_post = True
                else:
                    try:
                        last_posted_dt = datetime.fromisoformat(str(last_posted_at_raw).replace("Z", "+00:00"))
                        if last_posted_dt.tzinfo is None:
                            last_posted_dt = last_posted_dt.replace(tzinfo=timezone.utc)
                        elapsed = (now - last_posted_dt).total_seconds()
                        if elapsed >= interval_seconds:
                            should_post = True
                    except Exception:
                        should_post = True

                if should_post:
                    print(f"[AutoPost] Executing scheduled post '{post_key}' in guild '{guild.name}' ({guild.id})")
                    success, new_msg_id, now_iso, err = await execute_auto_post_send(guild, post_cfg)
                    if success:
                        post_cfg["last_message_id"] = new_msg_id
                        post_cfg["last_posted_at"] = now_iso
                        modified = True
                    else:
                        print(f"[AutoPost] Failed scheduled post '{post_key}': {err}")

        if modified:
            save_json(AUTO_POST_DB, data)

    except Exception as exc:
        print(f"[AutoPost] Error in background task loop: {exc}")


@auto_post_check_loop.before_loop
async def before_auto_post_check_loop():
    if _bot:
        await _bot.wait_until_ready()


def register(bot: commands.Bot):
    global _bot
    _bot = bot
