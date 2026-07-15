"""
modules/generate_key.py
Direct key generation commands for 1 Week Premium and 1 Week Regular keys.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands

import os

from modules.utils import load_json, save_json
from modules.key_panel import _request_key_panel_key

PREMIUM_WEBHOOK_URL = os.getenv("GENERATE_KEY_PREMIUM_WEBHOOK_URL", "")
PREMIUM_HMAC_SECRET = os.getenv("GENERATE_KEY_PREMIUM_HMAC_SECRET", "premium")
PREMIUM_HMAC_HEADER = os.getenv("GENERATE_KEY_PREMIUM_HMAC_HEADER", "seisen")

REGULAR_WEBHOOK_URL = os.getenv("GENERATE_KEY_REGULAR_WEBHOOK_URL", "")
REGULAR_HMAC_SECRET = os.getenv("GENERATE_KEY_REGULAR_HMAC_SECRET", "seisen")
REGULAR_HMAC_HEADER = os.getenv("GENERATE_KEY_REGULAR_HMAC_HEADER", "seisen")

COOLDOWN_DAYS = 7
CLAIMS_FILE = "generate_key_claims"


async def _handle_generate_key(
    interaction: discord.Interaction,
    webhook_url: str,
    hmac_secret: str,
    hmac_header: str,
    product_name: str,
    command_key: str,
):
    await interaction.response.defer(ephemeral=True)

    claims = load_json(CLAIMS_FILE, {})
    guild_id = str(interaction.guild.id) if interaction.guild else "dm"
    user_id = str(interaction.user.id)

    guild_claims = claims.setdefault(guild_id, {})
    cmd_claims = guild_claims.setdefault(command_key, {})
    user_claim = cmd_claims.get(user_id)

    now_utc = datetime.now(timezone.utc)

    if user_claim and isinstance(user_claim, dict):
        cached_key = user_claim.get("key")
        generated_at_str = user_claim.get("generated_at")
        if cached_key and generated_at_str:
            try:
                generated_at = datetime.fromisoformat(generated_at_str)
                if generated_at.tzinfo is None:
                    generated_at = generated_at.replace(tzinfo=timezone.utc)
                elapsed = now_utc - generated_at
                if elapsed < timedelta(days=COOLDOWN_DAYS):
                    expires_at = generated_at + timedelta(days=COOLDOWN_DAYS)
                    expires_ts = int(expires_at.timestamp())
                    embed = discord.Embed(
                        title="🔑 Your Key",
                        description=(
                            f"ℹ️ You already have an active **{product_name}** key.\n\n"
                            f"**Your Key:**\n```{cached_key}```\n"
                            f"⚠️ *Save this key — it will not be shown again once dismissed.*\n\n"
                            f"⏳ **Next key available:** <t:{expires_ts}:F> (<t:{expires_ts}:R>)\n\n"
                            f"🛑 Do not share your key with anyone."
                        ),
                        color=discord.Color.orange(),
                        timestamp=now_utc,
                    )
                    embed.set_footer(text="Keep this key private.")
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return
            except Exception:
                pass

    key_value, err = await _request_key_panel_key(webhook_url, hmac_secret, hmac_header, product_name)
    if not key_value:
        await interaction.followup.send(f"❌ Failed to generate key: {err}", ephemeral=True)
        return

    cmd_claims[user_id] = {"key": key_value, "generated_at": now_utc.isoformat()}
    save_json(CLAIMS_FILE, claims)

    expires_at = now_utc + timedelta(days=COOLDOWN_DAYS)
    expires_ts = int(expires_at.timestamp())
    embed = discord.Embed(
        title="🔑 Your Key",
        description=(
            f"Your **{product_name}** key has been generated!\n\n"
            f"**Your Key:**\n```{key_value}```\n"
            f"⚠️ *Save this key — it will not be shown again once dismissed.*\n\n"
            f"⏳ **Expires / Next key:** <t:{expires_ts}:F> (<t:{expires_ts}:R>)\n\n"
            f"🛑 Do not share your key with anyone."
        ),
        color=discord.Color.green(),
        timestamp=now_utc,
    )
    embed.set_footer(text="Keep this key private.")
    await interaction.followup.send(embed=embed, ephemeral=True)


@app_commands.command(name="generatekeypremium", description="Generate a 1 Week Premium key")
async def generatekeypremium(interaction: discord.Interaction):
    await _handle_generate_key(interaction, PREMIUM_WEBHOOK_URL, PREMIUM_HMAC_SECRET, PREMIUM_HMAC_HEADER, "1 Week Premium", "premium")


@app_commands.command(name="generatekeyregular", description="Generate a 1 Week Regular key")
async def generatekeyregular(interaction: discord.Interaction):
    await _handle_generate_key(interaction, REGULAR_WEBHOOK_URL, REGULAR_HMAC_SECRET, REGULAR_HMAC_HEADER, "1 Week Regular", "regular")


def register(bot: commands.Bot):
    bot.tree.add_command(generatekeypremium)
    bot.tree.add_command(generatekeyregular)
