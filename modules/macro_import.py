"""
modules/macro_import.py
Detects macro ".json" attachments posted in configured "public" channels/forum
posts and replies with a "Macro's File Import URL" embed (mirrors the Plusmate
bot's response). Channels marked "private" instead require the /import slash
command — its response is a real Discord ephemeral message (like /purge's
"Only you can see this"), which is the only way a bot reply can actually be
invisible to everyone but the user who triggered it; a plain message reply can
never be ephemeral, only an interaction (slash command/button/etc.) can.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional, Union
from urllib.parse import urlparse, parse_qs

import discord
from discord import app_commands
from discord.ext import commands

from modules.utils import load_json, save_json

MACRO_IMPORT_CONFIG_FILE = "macro_import_configs"

MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024  # 2 MB — macros are small JSON, this is generous

DEFAULT_CONFIG = {
    "enabled": True,
    "channel_ids": [],       # text channels, forum channels, and/or individual threads to watch (public replies)
    "private_channel_ids": [],  # channels where /import must be used instead — direct posts get deleted
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

    private_channel_ids = merged.get("private_channel_ids", [])
    merged["private_channel_ids"] = [str(c) for c in private_channel_ids] if isinstance(private_channel_ids, list) else []

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
    Returns an ordered, de-duplicated list of "UnitName" or "UnitName (Trait)"
    strings if `data` matches a known macro schema, or None if it doesn't look
    like a macro file at all.

    Schemas supported (produced by the Seisen macro recorder):
      A) {"version": 1, "entries": [{"values": [...], "slotUnitName": "...", "slotUnitTrait": "..."}]}
      B) [{"values": [...], "slotUnitName": "...", "slotUnitTrait": "..."}, ...] (raw entries array)
      C) {"<index>": {"Type": "...", "Pos": "UnitName - 1", ...}, ...}
    """
    order: list[str] = []
    traits: dict[str, Optional[str]] = {}

    def _add(name: Any, trait: Any = None) -> None:
        if isinstance(name, str) and name.strip():
            clean = name.strip()
            if clean not in traits:
                order.append(clean)
                traits[clean] = trait.strip() if isinstance(trait, str) and trait.strip() else None

    def _formatted() -> list[str]:
        return [f"{name} ({traits[name]})" if traits[name] else name for name in order]

    if not data:
        return None

    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("entries")
    else:
        return None

    if isinstance(entries, list):
        saw_action_entry = False
        for entry in entries:
            if isinstance(entry, dict) and "values" in entry:
                saw_action_entry = True
                _add(entry.get("slotUnitName"), entry.get("slotUnitTrait"))
        return _formatted() if saw_action_entry else None

    # Schema C: every top-level key is a numeric index mapping to an action dict.
    if isinstance(data, dict) and all(isinstance(k, str) and k.isdigit() for k in data.keys()) and all(
        isinstance(v, dict) and "Type" in v for v in data.values()
    ):
        for action in data.values():
            pos = action.get("Pos")
            if isinstance(pos, str):
                _add(_UNIT_SLOT_SUFFIX_RE.sub("", pos))
        return _formatted()

    return None


def _parse_attachment_expiry(url: str) -> Optional[int]:
    """Discord CDN attachment URLs carry a hex unix-timestamp `ex` query param."""
    try:
        query = parse_qs(urlparse(url).query)
        ex_hex = query.get("ex", [None])[0]
        return int(ex_hex, 16) if ex_hex else None
    except (ValueError, TypeError):
        return None


async def _read_macro_units(read_bytes) -> Optional[list[str]]:
    """Decode+validate raw attachment bytes; None means "not a recognized macro"."""
    try:
        data = json.loads(read_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _extract_required_units(data)


# ── Embed construction ────────────────────────────────────────────────────────

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


def _build_import_embed(url: str, units: list[str]) -> discord.Embed:
    """Leaner embed for /import's ephemeral reply — a fenced code block instead
    of inline code, since only fenced blocks get Discord's built-in hover
    "copy" icon on the code itself."""
    units_line = f"Required Unit: {', '.join(units)}." if units else "Required Unit: *(none detected)*"
    description = f"{units_line}\n```\n{url}\n```"
    return discord.Embed(title="Macro's File Import URL", description=description, color=discord.Color.blurple())


class CopyCdnLinkView(discord.ui.View):
    """One-off view attached to /import's ephemeral reply. The code block's
    hover-to-copy icon is desktop-only and easy to miss, so this gives an
    actual button — on any platform — that hands back the bare URL as plain
    text (easiest possible long-press/select-all copy target)."""

    def __init__(self, url: str):
        super().__init__(timeout=600)
        self._url = url

    @discord.ui.button(label="📋 Copy CDN Link", style=discord.ButtonStyle.primary)
    async def copy_cdn_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(self._url, ephemeral=True)


# ── Message handling (public channels only) ───────────────────────────────────

async def _handle_macro_attachment(message: discord.Message) -> None:
    if not message.guild or message.author.bot:
        return

    cfg = _guild_cfg(message.guild.id)
    if not cfg.get("enabled", True):
        return

    public_ids = set(cfg.get("channel_ids", []))
    private_ids = set(cfg.get("private_channel_ids", []))
    if not public_ids and not private_ids:
        return

    message_ids = _channel_and_parent_ids(message.channel)

    if message_ids & private_ids:
        # Private channels are /import-only — anything posted directly here
        # (macro or not) gets removed instead of processed.
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass
        return

    if not (message_ids & public_ids) or not message.attachments:
        return

    json_attachment = None
    units: Optional[list[str]] = None
    unsupported_filename: Optional[str] = None

    for attachment in message.attachments:
        if attachment.size > MAX_ATTACHMENT_BYTES:
            continue
        try:
            candidate_bytes = await attachment.read()
        except discord.HTTPException:
            continue
        candidate_units = await _read_macro_units(candidate_bytes)
        if candidate_units is not None:
            json_attachment = attachment
            units = candidate_units
            break
        elif attachment.filename.lower().endswith(".json"):
            unsupported_filename = attachment.filename

    if json_attachment is None or units is None:
        if unsupported_filename:
            embed = discord.Embed(
                title="⚠️ Unsupported Macro Format",
                description=(
                    f"The file `{unsupported_filename}` was recognized as JSON, but it does not match "
                    "any supported Seisen macro format.\n\n"
                    "**Supported Formats:**\n"
                    "• Version 1 JSON (`{\"version\": 1, \"entries\": [...]}`)\n"
                    "• Raw Entry Array (`[{\"values\": [...], ...}]`)\n"
                    "• Key-Indexed Actions (`{\"0\": {\"Type\": \"...\"}}`)\n\n"
                    "Please re-export your macro file using the official recorder."
                ),
                color=discord.Color.gold(),
            )
            await message.reply(embed=embed, delete_after=60)
        return

    embed = _build_embed(json_attachment.url, units)
    await message.reply(embed=embed, delete_after=60)


# ── Slash commands ────────────────────────────────────────────────────────────

macroimport_group = app_commands.Group(
    name="macroimport",
    description="Configure automatic macro-file import responses",
    # Hides /macroimport from the command list for anyone without Manage
    # Server by default — regular members only ever see /import. (Server
    # admins can still widen this per-role in Integrations settings.)
    default_permissions=discord.Permissions(manage_guild=True),
)


@macroimport_group.command(name="add-channel", description="Watch a channel, forum, or thread for macro files")
@app_commands.describe(
    channel="Text channel, forum, or thread to watch",
    private="If true, this channel is /import-only — direct posts get deleted, replies are ephemeral",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def macroimport_add_channel(
    interaction: discord.Interaction,
    channel: Union[discord.TextChannel, discord.ForumChannel, discord.Thread],
    private: bool = False,
):
    cfg = _guild_cfg(interaction.guild.id)
    channel_id = str(channel.id)
    target_list, other_list = ("private_channel_ids", "channel_ids") if private else ("channel_ids", "private_channel_ids")

    if channel_id in cfg[other_list]:
        cfg[other_list].remove(channel_id)
    if channel_id not in cfg[target_list]:
        cfg[target_list].append(channel_id)
    _save_guild_cfg(interaction.guild.id, cfg)

    mode = "private (/import only)" if private else "public"
    await interaction.response.send_message(
        f"✅ Now watching {channel.mention} for macro `.json` uploads — **{mode}**.", ephemeral=True
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
    removed = False
    for key in ("channel_ids", "private_channel_ids"):
        if channel_id in cfg[key]:
            cfg[key].remove(channel_id)
            removed = True
    if removed:
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
    private_channels = ", ".join(f"<#{c}>" for c in cfg["private_channel_ids"]) or "*(none)*"
    embed = discord.Embed(title="Macro Import Configuration", color=discord.Color.blurple())
    embed.add_field(name="Enabled", value=str(cfg["enabled"]), inline=True)
    embed.add_field(name="Watched Channels (Public)", value=channels, inline=False)
    embed.add_field(name="Watched Channels (Private / /import-only)", value=private_channels, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.command(name="import", description="Privately import a macro file — only you can see the reply")
@app_commands.describe(file="Your macro .json (or exported .txt) file")
async def import_macro(interaction: discord.Interaction, file: discord.Attachment):
    if not interaction.guild:
        await interaction.response.send_message("❌ This command only works in a server.", ephemeral=True)
        return

    cfg = _guild_cfg(interaction.guild.id)
    if not cfg.get("enabled", True):
        await interaction.response.send_message("❌ Macro import is disabled in this server.", ephemeral=True)
        return

    private_ids = set(cfg.get("private_channel_ids", []))
    if not private_ids:
        await interaction.response.send_message(
            "❌ No private macro-import channel is configured. Ask an admin to run "
            "`/macroimport add-channel private:true`.",
            ephemeral=True,
        )
        return

    if not (_channel_and_parent_ids(interaction.channel) & private_ids):
        await interaction.response.send_message(
            "❌ This command can only be used in a designated private macro-import channel.", ephemeral=True
        )
        return

    if file.size > MAX_ATTACHMENT_BYTES:
        await interaction.response.send_message("❌ That file is too large to be a macro.", ephemeral=True)
        return

    try:
        raw_bytes = await file.read()
    except discord.HTTPException:
        await interaction.response.send_message("❌ Couldn't read that file — try again.", ephemeral=True)
        return

    units = await _read_macro_units(raw_bytes)
    if units is None:
        await interaction.response.send_message("❌ That doesn't look like a valid macro file.", ephemeral=True)
        return

    embed = _build_import_embed(file.url, units)
    await interaction.response.send_message(embed=embed, view=CopyCdnLinkView(file.url), ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: commands.Bot):
    bot.tree.add_command(macroimport_group)
    bot.tree.add_command(import_macro)
    bot.add_listener(_handle_macro_attachment, "on_message")
