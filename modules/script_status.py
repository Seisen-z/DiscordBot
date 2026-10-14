"""
modules/script_status.py
Posts the "Supported Games" board (Working / Discontinued) to a channel and keeps it current.

The website builds its script list from GitHub (free/premium/discontinued lists) and exposes it at
``<SITE_URL>/api/scripts``. This module polls that endpoint, and whenever the set of working or
discontinued games changes it deletes the previous board and posts a fresh one.

Environment:
  SCRIPT_STATUS_CHANNEL_ID   Channel to post in (required, nothing is posted without it)
  SCRIPT_STATUS_GUILD_ID     Guild of that channel (optional, defaults to the bot's first guild)
  SITE_URL                   Website base URL (default https://seisen.vercel.app)
  SUPPORTED_EXECUTORS_URL    Optional link shown as a button under the board
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands, tasks

from modules.utils import load_json, save_json

_bot: commands.Bot | None = None
STATE_FILE = "script_status_state"
CHECK_INTERVAL_MINUTES = 5
FIELD_LIMIT = 1000  # Discord caps a field value at 1024 characters


def _site_url() -> str:
    return (os.getenv("SITE_URL") or "https://seisen.vercel.app").rstrip("/")


async def fetch_scripts() -> list[dict]:
    """Return the website's script list, or [] if it could not be fetched."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{_site_url()}/api/scripts", timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    print(f"[ScriptStatus] Site returned HTTP {resp.status}")
                    return []
                data = await resp.json()
    except Exception as exc:
        print(f"[ScriptStatus] Could not fetch scripts: {exc}")
        return []
    return data if isinstance(data, list) else []


def split_scripts(scripts: list[dict]) -> tuple[list[str], list[str]]:
    working: list[str] = []
    discontinued: list[str] = []
    for script in scripts:
        name = str(script.get("name") or "").strip()
        if not name:
            continue
        if script.get("status") == "Discontinued":
            discontinued.append(name)
        else:
            working.append(name)
    return working, discontinued


def signature(working: list[str], discontinued: list[str]) -> str:
    """Order-independent fingerprint, so only real status changes trigger a re-post."""
    return json.dumps({"working": sorted(working), "discontinued": sorted(discontinued)}, sort_keys=True)


def _add_list_field(embed: discord.Embed, title: str, emoji: str, names: list[str]):
    if not names:
        embed.add_field(name=title, value="None right now.", inline=False)
        return
    chunks: list[str] = []
    current = ""
    for name in names:
        line = f"{emoji} {name}\n"
        if len(current) + len(line) > FIELD_LIMIT:
            chunks.append(current.rstrip())
            current = ""
        current += line
    if current:
        chunks.append(current.rstrip())
    for i, chunk in enumerate(chunks):
        embed.add_field(name=title if i == 0 else "​", value=chunk, inline=False)


def build_embed(working: list[str], discontinued: list[str]) -> discord.Embed:
    embed = discord.Embed(
        title="🎮 SEISEN HUB — SUPPORTED GAMES",
        description="Below is the current compatibility list for Seisen Hub. Game support may change after game updates.",
        color=discord.Color.from_str("#c9a97a"),
        timestamp=datetime.now(timezone.utc),
    )
    _add_list_field(embed, "🟢 Supported & Working", "🟢", working)
    _add_list_field(embed, "🔴 Discontinued / Not Working", "🔴", discontinued)
    embed.add_field(
        name="⚠️ Before Using Seisen Hub",
        value=(
            "Make sure you're using a supported and updated executor. If a game recently updated, "
            "some features may temporarily stop working until Seisen Hub is updated."
        ),
        inline=False,
    )
    embed.set_footer(text="🟢 Working • 🔴 Discontinued / Not Working")
    return embed


def _build_view() -> discord.ui.View | None:
    url = (os.getenv("SUPPORTED_EXECUTORS_URL") or "").strip()
    if not url:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="View Supported Executors", url=url, style=discord.ButtonStyle.link))
    return view


async def _resolve_channel(guild: discord.Guild | None):
    channel_id = (os.getenv("SCRIPT_STATUS_CHANNEL_ID") or "").strip()
    if not channel_id.isdigit():
        return None
    channel_id = int(channel_id)
    channel = guild.get_channel(channel_id) if guild else None
    if channel is None and _bot:
        channel = _bot.get_channel(channel_id)
    if channel is None and _bot:
        try:
            channel = await _bot.fetch_channel(channel_id)
        except Exception:
            channel = None
    return channel if channel is not None and hasattr(channel, "send") else None


async def post_script_status(guild: discord.Guild | None = None, force: bool = False) -> tuple[bool, str]:
    """
    Post the board if the status list changed since the last post (or ``force``).
    Returns (posted, message).
    """
    channel = await _resolve_channel(guild)
    if channel is None:
        return False, "SCRIPT_STATUS_CHANNEL_ID is not set or the channel was not found."

    scripts = await fetch_scripts()
    if not scripts:
        return False, "Could not load the script list from the website."

    working, discontinued = split_scripts(scripts)
    sig = signature(working, discontinued)

    state = load_json(STATE_FILE, {}) or {}
    if not force and state.get("signature") == sig and state.get("last_message_id"):
        return False, "No changes."

    old_id = state.get("last_message_id")
    if old_id and str(old_id).isdigit():
        try:
            old = await channel.fetch_message(int(old_id))
            await old.delete()
        except Exception:
            pass

    message = await channel.send(embed=build_embed(working, discontinued), view=_build_view())
    state.update({
        "signature": sig,
        "last_message_id": str(message.id),
        "channel_id": str(channel.id),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    save_json(STATE_FILE, state)
    print(f"[ScriptStatus] Posted board: {len(working)} working, {len(discontinued)} discontinued")
    return True, "Posted."


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def script_status_check_loop():
    if _bot is None:
        return
    try:
        guild_id = (os.getenv("SCRIPT_STATUS_GUILD_ID") or "").strip()
        guild = _bot.get_guild(int(guild_id)) if guild_id.isdigit() else (_bot.guilds[0] if _bot.guilds else None)
        await post_script_status(guild)
    except Exception as exc:
        print(f"[ScriptStatus] Check failed: {exc}")


@script_status_check_loop.before_loop
async def _before_script_status_check_loop():
    if _bot:
        await _bot.wait_until_ready()


def register(bot: commands.Bot):
    global _bot
    _bot = bot
