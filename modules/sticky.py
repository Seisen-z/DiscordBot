"""
modules/sticky.py
Sticky messages – config, modal, slash commands, and message handler logic.
"""

from __future__ import annotations

import discord
from discord import app_commands

from modules.utils import load_json, save_json, STICKY_FILE


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_sticky() -> dict:
    return load_json(STICKY_FILE, {})


def save_sticky(data: dict):
    save_json(STICKY_FILE, data)


# ── Modal ─────────────────────────────────────────────────────────────────────

class StickyModal(discord.ui.Modal, title="Sticky Message Setup"):
    sticky_title = discord.ui.TextInput(
        label="Title",
        placeholder="Sticky message title (optional)",
        required=False,
        max_length=256,
    )
    sticky_content = discord.ui.TextInput(
        label="Content",
        style=discord.TextStyle.paragraph,
        placeholder="Sticky message content...",
        max_length=2000,
    )
    color = discord.ui.TextInput(
        label="Embed Color (hex, optional)",
        placeholder="#5865F2",
        required=False,
        max_length=7,
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        title = self.sticky_title.value or None
        content = self.sticky_content.value
        color_str = self.color.value.strip() if self.color.value else None
        try:
            color = int(color_str.lstrip("#"), 16) if color_str else 0x5865F2
        except Exception:
            color = 0x5865F2

        embed = discord.Embed(title=title, description=content, color=color)
        sticky = load_sticky()
        guild_id = str(interaction.guild.id)
        channel_id = str(self.channel.id)

        old_sticky = sticky.get(guild_id, {}).get(channel_id, {})
        last_message_id = old_sticky.get("last_message_id") if isinstance(old_sticky, dict) else None

        deleted = False
        if last_message_id:
            try:
                await self.channel.get_partial_message(last_message_id).delete()
                deleted = True
            except (discord.NotFound, discord.HTTPException):
                pass

        if not deleted:
            old_title = old_sticky.get("title") if isinstance(old_sticky, dict) else None
            old_content = old_sticky.get("content", "") if isinstance(old_sticky, dict) else old_sticky
            async for msg in self.channel.history(limit=20):
                if msg.author.id == interaction.guild.me.id:
                    if old_title and msg.embeds and msg.embeds[0].title == old_title and msg.embeds[0].description == old_content:
                        await msg.delete()
                        break
                    elif old_content and msg.content == old_content:
                        await msg.delete()
                        break

        new_msg = await self.channel.send(embed=embed)
        sticky.setdefault(guild_id, {})[channel_id] = {
            "title": title, "content": content, "color": color, "last_message_id": new_msg.id
        }
        save_sticky(sticky)
        await interaction.response.send_message(f"✅ Sticky embed set in {self.channel.mention}", ephemeral=True)


# ── Message handler ────────────────────────────────────────────────────────────

async def handle_sticky_message(message: discord.Message, bot_user: discord.ClientUser):
    """Called from on_message – re-posts sticky if configured for this channel."""
    if not message.guild or message.author.id == bot_user.id:
        return
    sticky = load_sticky()
    guild_id = str(message.guild.id)
    channel_id = str(message.channel.id)
    if guild_id not in sticky or channel_id not in sticky[guild_id]:
        return

    sticky_cfg = sticky[guild_id][channel_id]
    sticky_title = sticky_cfg.get("title") if isinstance(sticky_cfg, dict) else None
    sticky_msg = sticky_cfg.get("content", "") if isinstance(sticky_cfg, dict) else sticky_cfg
    sticky_color = sticky_cfg.get("color", 0x5865F2) if isinstance(sticky_cfg, dict) else 0x5865F2
    last_message_id = sticky_cfg.get("last_message_id") if isinstance(sticky_cfg, dict) else None

    deleted = False
    if last_message_id:
        try:
            await message.channel.get_partial_message(last_message_id).delete()
            deleted = True
        except (discord.NotFound, discord.HTTPException):
            pass

    if not deleted:
        async for msg in message.channel.history(limit=20):
            if msg.author.id == bot_user.id:
                if sticky_title and msg.embeds and msg.embeds[0].title == sticky_title and msg.embeds[0].description == sticky_msg:
                    await msg.delete()
                    break
                elif not sticky_title and msg.content == sticky_msg:
                    await msg.delete()
                    break

    if isinstance(sticky_cfg, dict):
        embed = discord.Embed(title=sticky_title, description=sticky_msg, color=sticky_color)
        new_msg = await message.channel.send(embed=embed)
    else:
        new_msg = await message.channel.send(sticky_msg)

    if isinstance(sticky_cfg, dict):
        sticky[guild_id][channel_id]["last_message_id"] = new_msg.id
    else:
        sticky[guild_id][channel_id] = {
            "content": sticky_msg, "title": None, "color": 0x5865F2, "last_message_id": new_msg.id
        }
    save_sticky(sticky)


# ── Slash commands ────────────────────────────────────────────────────────────

sticky_group = app_commands.Group(name="sticky", description="Sticky message management")


@sticky_group.command(name="message", description="Set a sticky message in a channel (modal)")
@app_commands.describe(channel="Channel to set the sticky message in")
@app_commands.checks.has_permissions(manage_guild=True)
async def sticky_message(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.send_modal(StickyModal(channel))


@sticky_group.command(name="remove", description="Remove the sticky message from a channel")
@app_commands.describe(channel="Channel to remove the sticky message from")
@app_commands.checks.has_permissions(manage_guild=True)
async def sticky_remove(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    sticky = load_sticky()
    guild_id = str(interaction.guild.id)
    channel_id = str(channel.id)
    if guild_id not in sticky or channel_id not in sticky[guild_id]:
        await interaction.followup.send(f"❌ No sticky message set in {channel.mention}", ephemeral=True)
        return

    sticky_cfg = sticky[guild_id][channel_id]
    sticky_msg = sticky_cfg.get("content", "") if isinstance(sticky_cfg, dict) else sticky_cfg
    sticky_title = sticky_cfg.get("title") if isinstance(sticky_cfg, dict) else None
    last_message_id = sticky_cfg.get("last_message_id") if isinstance(sticky_cfg, dict) else None

    deleted = False
    if last_message_id:
        try:
            await channel.get_partial_message(last_message_id).delete()
            deleted = True
        except (discord.NotFound, discord.HTTPException):
            pass

    if not deleted:
        async for msg in channel.history(limit=20):
            if msg.author.id == interaction.guild.me.id:
                if sticky_title and msg.embeds and msg.embeds[0].description == sticky_msg:
                    await msg.delete()
                    break
                elif not sticky_title and msg.content == sticky_msg:
                    await msg.delete()
                    break

    del sticky[guild_id][channel_id]
    if not sticky[guild_id]:
        del sticky[guild_id]
    save_sticky(sticky)
    await interaction.followup.send(f"✅ Sticky message removed from {channel.mention}", ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: discord.ext.commands.Bot):
    bot.tree.add_command(sticky_group)

    @bot.listen("on_message")
    async def _sticky_on_message(message: discord.Message) -> None:
        await handle_sticky_message(message, bot.user)
