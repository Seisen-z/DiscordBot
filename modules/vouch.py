"""
modules/vouch.py
Vouch system – config I/O and slash commands.
"""

from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands

from modules.utils import load_json, save_json, VOUCH_CONFIG_FILE


def load_vouch_config() -> dict:
    return load_json(VOUCH_CONFIG_FILE, {})


def save_vouch_config(data: dict):
    save_json(VOUCH_CONFIG_FILE, data)


def _register_commands(bot: discord.ext.commands.Bot):

    @bot.tree.command(name="vouch_setup", description="Set the channel where vouches will be posted")
    @app_commands.describe(channel="The text channel to send vouches to")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def vouch_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        cfg = load_vouch_config()
        guild_id = str(interaction.guild_id)
        current = cfg.get(guild_id)
        count = current.get("count", 0) if isinstance(current, dict) else 0
        cfg[guild_id] = {"channel_id": channel.id, "count": count}
        save_vouch_config(cfg)
        await interaction.response.send_message(f"✅ Vouch channel has been set to {channel.mention}", ephemeral=True)

    @bot.tree.command(name="vouch", description="Create a new vouch for this discord server!")
    @app_commands.describe(
        message="Your vouch message",
        stars="Rating from 1 to 5 stars",
        proof="Optional image proof for your vouch",
    )
    @app_commands.choices(stars=[
        app_commands.Choice(name="⭐", value=1),
        app_commands.Choice(name="⭐⭐", value=2),
        app_commands.Choice(name="⭐⭐⭐", value=3),
        app_commands.Choice(name="⭐⭐⭐⭐", value=4),
        app_commands.Choice(name="⭐⭐⭐⭐⭐", value=5),
    ])
    async def vouch(
        interaction: discord.Interaction,
        message: str,
        stars: int,
        proof: discord.Attachment = None,
    ):
        cfg = load_vouch_config()
        guild_id = str(interaction.guild_id)
        guild_data = cfg.get(guild_id)

        if not guild_data:
            await interaction.response.send_message(
                "❌ The vouch system is not set up yet. An admin needs to run `/vouch_setup`.", ephemeral=True
            )
            return

        if not isinstance(guild_data, dict):
            # Legacy format: guild_data was just the channel_id (str or int)
            try:
                channel_id = int(guild_data)
                count = 0
                guild_data = {"channel_id": channel_id, "count": count}
                cfg[guild_id] = guild_data
            except (ValueError, TypeError):
                await interaction.response.send_message("❌ The vouch system is not set up properly.", ephemeral=True)
                return

        channel_id = guild_data.get("channel_id")
        count = guild_data.get("count", 0)

        if not channel_id:
            await interaction.response.send_message("❌ The vouch system is not set up properly.", ephemeral=True)
            return

        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            await interaction.response.send_message(
                "❌ The configured vouch channel no longer exists. Please ask an admin to re-run `/vouch_setup`.",
                ephemeral=True,
            )
            return

        if interaction.channel_id != channel_id:
            await interaction.response.send_message(
                f"❌ Please use this command in the <#{channel_id}> channel.", ephemeral=True
            )
            return

        count += 1
        cfg[guild_id]["count"] = count

        star_str = "⭐" * stars
        embed = discord.Embed(
            title=f"New vouch for {interaction.guild.name} created!",
            color=discord.Color.from_rgb(46, 137, 255),
            timestamp=datetime.now(timezone.utc),
        )
        embed.description = f"{star_str}\n\n**Vouch:**\n{message}"
        vouched_at_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        embed.add_field(name="Vouch Nº:", value=str(count), inline=True)
        embed.add_field(name="Vouched by:", value=interaction.user.mention, inline=True)
        embed.add_field(name="Vouched at:", value=vouched_at_str, inline=True)

        if proof:
            if proof.content_type and proof.content_type.startswith("image/"):
                embed.set_image(url=proof.url)
            else:
                await interaction.response.send_message("❌ The proof must be an image.", ephemeral=True)
                return

        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"✨ {interaction.guild.name} Script")

        try:
            await interaction.response.send_message(embed=embed)
            save_vouch_config(cfg)
            msg = await interaction.original_response()
            try:
                await msg.add_reaction("❤️")
            except discord.HTTPException:
                pass
        except discord.HTTPException:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Failed to post vouch due to missing permissions.", ephemeral=True
                )


def register(bot: discord.ext.commands.Bot):
    _register_commands(bot)
