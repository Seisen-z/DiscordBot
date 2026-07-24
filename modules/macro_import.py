"""
modules/macro_import.py
Detects macro ".json" attachments posted in configured channels/forum posts and
replies with a "Macro's File Import URL" embed (mirrors the Plusmate bot's
response), including a button that re-uploads the file to a dedicated storage
channel to mint a fresh, longer-lived Discord CDN link.
"""

from __future__ import annotations

import io
import json
import re
import uuid
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import urlparse, parse_qs

import discord
from discord import app_commands
from discord.ext import commands

from modules.utils import load_json, save_json

MACRO_IMPORT_CONFIG_FILE = "macro_import_configs"
MACRO_IMPORT_CACHE_FILE = "macro_import_cache"

# Raw copies of validated macro attachments live here so the "make permanent"
# button keeps working even after the original Discord CDN link expires.
_MACRO_FILES_DIR = Path(__file__).resolve().parent.parent / "database" / "macro_files"

MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024  # 2 MB — macros are small JSON, this is generous

DEFAULT_CONFIG = {
    "enabled": True,
    "channel_ids": [],       # text channels, forum channels, and/or individual threads to watch
    "storage_channel_id": None,  # where the bot re-uploads the file to mint a fresh CDN link
}

_UNIT_SLOT_SUFFIX_RE = re.compile(r"\s*-\s*\d+\s*$")


# ── Config I/O ─────────────────────────────────────────────────────────────────

def _guild_cfg(guild_id: int) -> dict:
    root = load_json(MACRO_IMPORT_CONFIG_FILE, {})
    if not isinstance(root, dict):
        root = {}
    cfg = root.get(str(guild_id), {})
    if not isinstance(cfg, dict):
        cfg = {}
    merged = {**DEFAULT_CONFIG, **cfg}
    merged["enabled"] = bool(merged.get("enabled", True))

    channel_ids = merged.get("channel_ids", [])
    merged["channel_ids"] = [str(c) for c in channel_ids] if isinstance(channel_ids, list) else []

    storage_id = merged.get("storage_channel_id")
    merged["storage_channel_id"] = str(storage_id) if storage_id else None
    return merged


def _save_guild_cfg(guild_id: int, cfg: dict) -> None:
    root = load_json(MACRO_IMPORT_CONFIG_FILE, {})
    if not isinstance(root, dict):
        root = {}
    root[str(guild_id)] = cfg
    save_json(MACRO_IMPORT_CONFIG_FILE, root)


def _channel_and_parent_ids(channel: Any) -> set[str]:
    ids = {str(channel.id)}
    parent = getattr(channel, "parent", None)
    if parent is not None:
        ids.add(str(parent.id))
    return ids


# ── Macro JSON validation / unit extraction ───────────────────────────────────

def _extract_required_units(data: Any) -> Optional[list[str]]:
    """
    Returns an ordered, de-duplicated list of unit names if `data` matches a
    known macro schema, or None if it doesn't look like a macro file at all.

    Two schemas are supported (both produced by the Seisen macro recorder):
      A) {"version": 1, "entries": [{"values": [...], "slotUnitName": "..."}]}
      B) {"<index>": {"Type": "...", "Pos": "UnitName - 1", ...}, ...}
    """
    units: list[str] = []
    seen: set[str] = set()

    def _add(name: Any) -> None:
        if isinstance(name, str) and name.strip():
            clean = name.strip()
            if clean not in seen:
                seen.add(clean)
                units.append(clean)

    if not isinstance(data, dict) or not data:
        return None

    entries = data.get("entries")
    if isinstance(entries, list):
        saw_action_entry = False
        for entry in entries:
            if isinstance(entry, dict) and "values" in entry:
                saw_action_entry = True
                _add(entry.get("slotUnitName"))
        return units if saw_action_entry else None

    # Schema B: every top-level key is a numeric index mapping to an action dict.
    if all(isinstance(k, str) and k.isdigit() for k in data.keys()) and all(
        isinstance(v, dict) and "Type" in v for v in data.values()
    ):
        for action in data.values():
            pos = action.get("Pos")
            if isinstance(pos, str):
                _add(_UNIT_SLOT_SUFFIX_RE.sub("", pos))
        return units

    return None


def _parse_attachment_expiry(url: str) -> Optional[int]:
    """Discord CDN attachment URLs carry a hex unix-timestamp `ex` query param."""
    try:
        query = parse_qs(urlparse(url).query)
        ex_hex = query.get("ex", [None])[0]
        return int(ex_hex, 16) if ex_hex else None
    except (ValueError, TypeError):
        return None


# ── Embed / view construction ─────────────────────────────────────────────────

def _build_embed(url: str, units: list[str]) -> discord.Embed:
    expiry_ts = _parse_attachment_expiry(url)
    expiry_line = (
        f"This URL will expire <t:{expiry_ts}:R>, which will prevent you from importing the macro via this link.\n"
        if expiry_ts
        else "This URL will expire, which will prevent you from importing the macro via this link.\n"
    )
    units_line = f"Required Unit: {', '.join(units)}." if units else "Required Unit: *(none detected)*"

    description = (
        f"{expiry_line}"
        "If it expires, you need to get a new Import URL by re-uploading the macro or copying the download link "
        "from the original macro file.\n\n"
        f"{units_line}\n\n"
        f"```\n{url}\n```\n"
        f"For mobile users: [Click here]({url}) to access the file."
    )
    return discord.Embed(title="Macro's File Import URL", description=description, color=discord.Color.blurple())


class MacroUploadView(discord.ui.View):
    """Single shared persistent view; the button looks up which file it belongs
    to via the cache entry keyed by the message it's attached to."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Upload Macro (Make the Import-URL Permanent)",
        style=discord.ButtonStyle.secondary,
        custom_id="macro_import:upload_permanent",
    )
    async def upload_permanent(self, interaction: discord.Interaction, button: discord.ui.Button):
        cache = load_json(MACRO_IMPORT_CACHE_FILE, {})
        entry = cache.get(str(interaction.message.id))
        if not isinstance(entry, dict):
            await interaction.response.send_message(
                "❌ This macro file is no longer available for re-upload.", ephemeral=True
            )
            return

        cfg = _guild_cfg(interaction.guild.id)
        storage_channel_id = cfg.get("storage_channel_id")
        if not storage_channel_id:
            await interaction.response.send_message(
                "❌ No storage channel is configured for this server. An admin needs to run "
                "`/macroimport setup` first.",
                ephemeral=True,
            )
            return

        storage_channel = interaction.guild.get_channel(int(storage_channel_id))
        if storage_channel is None or not hasattr(storage_channel, "send"):
            await interaction.response.send_message(
                "❌ The configured storage channel could not be found.", ephemeral=True
            )
            return

        stored_path = _MACRO_FILES_DIR / entry.get("stored_file", "")
        if not stored_path.is_file():
            await interaction.response.send_message(
                "❌ The original macro file is no longer cached and can't be re-uploaded.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        raw_bytes = stored_path.read_bytes()
        file_name = entry.get("file_name", "macro.json")
        new_message = await storage_channel.send(
            content=f"📎 Macro re-upload (permanent host) — originally posted by <@{entry.get('author_id')}>",
            file=discord.File(io.BytesIO(raw_bytes), filename=file_name),
        )
        new_attachment = new_message.attachments[0]

        new_embed = _build_embed(new_attachment.url, entry.get("required_units", []))
        await interaction.message.edit(embed=new_embed, view=self)
        await interaction.followup.send("✅ Re-uploaded — the Import URL has been refreshed.", ephemeral=True)


# ── Message handling ───────────────────────────────────────────────────────────

async def _handle_macro_attachment(message: discord.Message) -> None:
    if not message.guild or message.author.bot or not message.attachments:
        return

    cfg = _guild_cfg(message.guild.id)
    if not cfg.get("enabled", True) or not cfg.get("channel_ids"):
        return
    if not _channel_and_parent_ids(message.channel) & set(cfg["channel_ids"]):
        return

    json_attachment = next(
        (a for a in message.attachments if a.filename.lower().endswith(".json")), None
    )
    if json_attachment is None or json_attachment.size > MAX_ATTACHMENT_BYTES:
        return

    try:
        raw_bytes = await json_attachment.read()
        data = json.loads(raw_bytes.decode("utf-8"))
    except (discord.HTTPException, UnicodeDecodeError, json.JSONDecodeError):
        return

    units = _extract_required_units(data)
    if units is None:
        return  # doesn't match a known macro schema — leave the message alone

    _MACRO_FILES_DIR.mkdir(parents=True, exist_ok=True)
    stored_file = f"{uuid.uuid4().hex}_{json_attachment.filename}"
    (_MACRO_FILES_DIR / stored_file).write_bytes(raw_bytes)

    embed = _build_embed(json_attachment.url, units)
    reply = await message.reply(embed=embed, view=MacroUploadView())

    cache = load_json(MACRO_IMPORT_CACHE_FILE, {})
    cache[str(reply.id)] = {
        "guild_id": str(message.guild.id),
        "stored_file": stored_file,
        "file_name": json_attachment.filename,
        "required_units": units,
        "author_id": str(message.author.id),
    }
    save_json(MACRO_IMPORT_CACHE_FILE, cache)


# ── Slash commands ────────────────────────────────────────────────────────────

macroimport_group = app_commands.Group(
    name="macroimport", description="Configure automatic macro-file import responses"
)


@macroimport_group.command(name="setup", description="Set the storage channel used to re-host macro files")
@app_commands.describe(storage_channel="Channel the bot re-uploads macro files to for a fresh, longer-lived link")
@app_commands.checks.has_permissions(manage_guild=True)
async def macroimport_setup(interaction: discord.Interaction, storage_channel: discord.TextChannel):
    cfg = _guild_cfg(interaction.guild.id)
    cfg["storage_channel_id"] = str(storage_channel.id)
    _save_guild_cfg(interaction.guild.id, cfg)
    await interaction.response.send_message(
        f"✅ Macro re-uploads will be hosted in {storage_channel.mention}.", ephemeral=True
    )


@macroimport_group.command(name="add-channel", description="Watch a channel, forum, or thread for macro files")
@app_commands.describe(channel="Text channel, forum, or thread to watch")
@app_commands.checks.has_permissions(manage_guild=True)
async def macroimport_add_channel(
    interaction: discord.Interaction,
    channel: Union[discord.TextChannel, discord.ForumChannel, discord.Thread],
):
    cfg = _guild_cfg(interaction.guild.id)
    channel_id = str(channel.id)
    if channel_id not in cfg["channel_ids"]:
        cfg["channel_ids"].append(channel_id)
        _save_guild_cfg(interaction.guild.id, cfg)
    await interaction.response.send_message(
        f"✅ Now watching {channel.mention} for macro `.json` uploads.", ephemeral=True
    )


@macroimport_group.command(name="remove-channel", description="Stop watching a channel, forum, or thread")
@app_commands.describe(channel="Text channel, forum, or thread to stop watching")
@app_commands.checks.has_permissions(manage_guild=True)
async def macroimport_remove_channel(
    interaction: discord.Interaction,
    channel: Union[discord.TextChannel, discord.ForumChannel, discord.Thread],
):
    cfg = _guild_cfg(interaction.guild.id)
    channel_id = str(channel.id)
    if channel_id in cfg["channel_ids"]:
        cfg["channel_ids"].remove(channel_id)
        _save_guild_cfg(interaction.guild.id, cfg)
        msg = f"✅ Stopped watching {channel.mention}."
    else:
        msg = f"❌ {channel.mention} wasn't being watched."
    await interaction.response.send_message(msg, ephemeral=True)


@macroimport_group.command(name="toggle", description="Enable or disable macro-import responses")
@app_commands.describe(enabled="Whether macro-import responses should be active")
@app_commands.checks.has_permissions(manage_guild=True)
async def macroimport_toggle(interaction: discord.Interaction, enabled: bool):
    cfg = _guild_cfg(interaction.guild.id)
    cfg["enabled"] = enabled
    _save_guild_cfg(interaction.guild.id, cfg)
    await interaction.response.send_message(
        f"✅ Macro-import responses are now **{'enabled' if enabled else 'disabled'}**.", ephemeral=True
    )


@macroimport_group.command(name="status", description="Show the current macro-import configuration")
@app_commands.checks.has_permissions(manage_guild=True)
async def macroimport_status(interaction: discord.Interaction):
    cfg = _guild_cfg(interaction.guild.id)
    channels = ", ".join(f"<#{c}>" for c in cfg["channel_ids"]) or "*(none)*"
    storage = f"<#{cfg['storage_channel_id']}>" if cfg["storage_channel_id"] else "*(not set)*"
    embed = discord.Embed(title="Macro Import Configuration", color=discord.Color.blurple())
    embed.add_field(name="Enabled", value=str(cfg["enabled"]), inline=True)
    embed.add_field(name="Storage Channel", value=storage, inline=True)
    embed.add_field(name="Watched Channels", value=channels, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: commands.Bot):
    bot.tree.add_command(macroimport_group)
    bot.add_view(MacroUploadView())
    bot.add_listener(_handle_macro_attachment, "on_message")
