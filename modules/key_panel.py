"""
modules/key_panel.py
Key generator panels with Junkie Dev webhook integration.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import os
from typing import Any, Dict, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from modules.utils import load_json, save_json, _as_int


# ── Webhook Request Helper ───────────────────────────────────────────────────

async def _request_key_panel_key(
    webhook_url: str,
    secret: str | None,
    header_name: str | None,
    product_name: str,
) -> tuple[str | None, str | None]:
    if not webhook_url:
        return None, "No webhook URL configured."

    payload = {"item": {"product": {"name": product_name}, "quantity": 1}}
    body = json.dumps(payload).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if secret:
        digest = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256).hexdigest()
        h_name = str(header_name or "seisen").strip() or "seisen"
        headers[h_name] = digest
        headers["X-Signature"] = digest
        # Add signatures for compatibility with various webhook configurations
        for key in ["X-Signature", "x-jnkie-signature", "signature", "jnkie-signature",
                    "Authorization", "X-Hub-Signature", "X-Hub-Signature-256",
                    "Discord-Boosting", "X-Discord-Boosting", "Discord-Boosting-Signature", "DiscordBoosting"]:
            headers[key] = digest

    def _parse_key(text: str) -> str | None:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                keys = data.get("keys")
                if isinstance(keys, list) and keys:
                    return str(keys[0] or "").strip() or None
                if data.get("key"):
                    return str(data["key"]).strip() or None
            elif isinstance(data, list) and data:
                return str(data[0] or "").strip() or None
        except json.JSONDecodeError:
            stripped = text.strip()
            if stripped and not stripped.startswith("<"):
                return stripped
        return None

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(webhook_url, json=payload, headers=headers) as resp:
                text = await resp.text()
                if resp.status == 200:
                    k = _parse_key(text)
                    return (k, None) if k else (None, "200 OK without a key in response.")
                if resp.status == 204:
                    return None, "204 No Content."
                short = text.strip()[:220] if text else ""
                return None, f"status {resp.status}" + (f" - {short}" if short else "")
    except Exception as e:
        return None, f"Webhook request failed: {e}"


# ── Views ────────────────────────────────────────────────────────────────────

class KeyPanelButtonView(discord.ui.View):
    def __init__(self, button_label: str = "Generate Key"):
        super().__init__(timeout=None)
        for child in self.children:
            if getattr(child, "custom_id", None) == "key_panel:generate":
                child.label = button_label

    @discord.ui.button(label="Generate Key", style=discord.ButtonStyle.success, custom_id="key_panel:generate")
    async def generate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        message = interaction.message
        if not guild or not message:
            await interaction.followup.send("❌ Error: Missing context.", ephemeral=True)
            return

        panels = load_json("key_panels", {})
        guild_panels = panels.get(str(guild.id), {})
        panel_cfg = guild_panels.get(str(message.id))

        if not panel_cfg:
            await interaction.followup.send("❌ This panel configuration was not found.", ephemeral=True)
            return

        # Check role requirements if configured (supports both legacy single role and multiple roles)
        required_role_ids = panel_cfg.get("required_role_ids") or []
        legacy_role_id = panel_cfg.get("required_role_id")
        if legacy_role_id and legacy_role_id not in required_role_ids:
            required_role_ids = list(required_role_ids) + [legacy_role_id]

        if required_role_ids:
            req_role_ids = {int(rid) for rid in required_role_ids}
            user_role_ids = {r.id for r in interaction.user.roles}
            if not (user_role_ids & req_role_ids):
                mentions = []
                for rid in required_role_ids:
                    role = guild.get_role(int(rid))
                    if role:
                        mentions.append(role.mention)
                    else:
                        mentions.append(f"Deleted Role ({rid})")
                role_mentions_str = " or ".join(mentions)
                await interaction.followup.send(f"❌ You must have at least one of the required roles ({role_mentions_str}) to generate keys from this panel.", ephemeral=True)
                return

        webhook_url = panel_cfg.get("webhook_url") or os.getenv("KEY_PANEL_WEBHOOK_URL") or os.getenv("WEBHOOK_URL")
        secret = panel_cfg.get("webhook_secret") or os.getenv("KEY_PANEL_WEBHOOK_HMAC_SECRET") or os.getenv("WEBHOOK_HMAC_SECRET")
        header = panel_cfg.get("webhook_hmac_header") or os.getenv("KEY_PANEL_WEBHOOK_HMAC_HEADER") or os.getenv("WEBHOOK_HMAC_HEADER") or "seisen"
        product_name = panel_cfg.get("product_name") or os.getenv("KEY_PANEL_PRODUCT_NAME") or "Premium Key"

        if not webhook_url:
            await interaction.followup.send("❌ Error: Key generation webhook is not configured for this panel or bot.", ephemeral=True)
            return

        # Claims check (cached for 30 days)
        claims = load_json("key_panel_claims", {})
        guild_claims = claims.setdefault(str(guild.id), {})
        panel_claims = guild_claims.setdefault(str(message.id), {})
        user_claim = panel_claims.get(str(interaction.user.id))

        now_utc = datetime.now(timezone.utc)
        use_cached_key = False
        cached_key = None
        expires_timestamp = None

        if user_claim and isinstance(user_claim, dict):
            cached_key = user_claim.get("key")
            generated_at_str = user_claim.get("generated_at")
            if cached_key and generated_at_str:
                try:
                    generated_at = datetime.fromisoformat(generated_at_str)
                    if generated_at.tzinfo is None:
                        generated_at = generated_at.replace(tzinfo=timezone.utc)
                    
                    elapsed = now_utc - generated_at
                    if elapsed < timedelta(days=30):
                        use_cached_key = True
                        expires_at = generated_at + timedelta(days=30)
                        expires_timestamp = int(expires_at.timestamp())
                except Exception:
                    pass

        if use_cached_key and cached_key:
            key_value = cached_key
            embed_desc = (
                f"ℹ️ You already have an active key for **{product_name}**.\n\n"
                f"**Your Key:**\n"
                f"```{key_value}```\n"
                f"⚠️ *Please save this key in a safe place. It will not be shown again once you dismiss this message.*\n\n"
                f"⏳ **Next Key Available:** <t:{expires_timestamp}:F> (<t:{expires_timestamp}:R>)\n\n"
                f"🛑 **Warning:** Do not share your key with anyone. Sharing your key may result in a warning or further action being taken against your account."
            )
        else:
            key_value, err = await _request_key_panel_key(webhook_url, secret, header, product_name)
            if not key_value:
                await interaction.followup.send(f"❌ Failed to generate key: {err}", ephemeral=True)
                return

            # Save claims
            panel_claims[str(interaction.user.id)] = {
                "key": key_value,
                "generated_at": now_utc.isoformat()
            }
            save_json("key_panel_claims", claims)

            expires_at = now_utc + timedelta(days=30)
            expires_timestamp = int(expires_at.timestamp())
            embed_desc = (
                f"Your key for **{product_name}** has been generated successfully.\n\n"
                f"**Your Key:**\n"
                f"```{key_value}```\n"
                f"⚠️ *Please save this key in a safe place. It will not be shown again once you dismiss this message.*\n\n"
                f"⏳ **Expires / Next Key:** <t:{expires_timestamp}:F> (<t:{expires_timestamp}:R>)\n\n"
                f"🛑 **Warning:** Do not share your key with anyone. Sharing your key may result in a warning or further action being taken against your account."
            )

        embed = discord.Embed(
            title="🔑 Your Generated License Key",
            description=embed_desc,
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Keep this key private and do not share it.")
        
        await interaction.followup.send(embed=embed, ephemeral=True)


# ── Slash Commands ───────────────────────────────────────────────────────────

keypanel_group = app_commands.Group(name="keypanel", description="Manage key generator panels")


@keypanel_group.command(name="setup", description="Create a key generator panel in a channel")
@app_commands.describe(
    panel_channel="Channel where the key panel embed will be posted",
    title="Embed title",
    description="Embed description (supports markdown)",
    button_label="Label of the button (default: Generate Key)",
    role1="1st required role (optional)",
    role2="2nd required role (optional)",
    role3="3rd required role (optional)",
    role4="4th required role (optional)",
    role5="5th required role (optional)"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def keypanel_setup(
    interaction: discord.Interaction,
    panel_channel: discord.TextChannel,
    title: str,
    description: str,
    button_label: str = "Generate Key",
    role1: discord.Role = None,
    role2: discord.Role = None,
    role3: discord.Role = None,
    role4: discord.Role = None,
    role5: discord.Role = None,
):
    await interaction.response.defer(ephemeral=True)

    resolved_url = os.getenv("KEY_PANEL_WEBHOOK_URL") or os.getenv("WEBHOOK_URL")
    if not resolved_url:
        await interaction.followup.send("❌ Error: No default webhook configured. Please set KEY_PANEL_WEBHOOK_URL or WEBHOOK_URL in your .env file.", ephemeral=True)
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    product_name = os.getenv("KEY_PANEL_PRODUCT_NAME") or "Premium Key"
    embed.set_footer(text=f"Product: {product_name}")

    view = KeyPanelButtonView(button_label=button_label)
    try:
        panel_msg = await panel_channel.send(embed=embed, view=view)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to send message to channel: {e}", ephemeral=True)
        return

    panels = load_json("key_panels", {})
    guild_id = str(interaction.guild.id)
    if guild_id not in panels:
        panels[guild_id] = {}

    roles = [r for r in [role1, role2, role3, role4, role5] if r]
    panels[guild_id][str(panel_msg.id)] = {
        "channel_id": panel_channel.id,
        "message_id": panel_msg.id,
        "title": title,
        "description": description,
        "button_label": button_label,
        "required_role_ids": [str(r.id) for r in roles],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": interaction.user.id
    }
    save_json("key_panels", panels)

    await interaction.followup.send(f"✅ Key panel created successfully in {panel_channel.mention}!", ephemeral=True)


@keypanel_group.command(name="list", description="List all key panels created in this server")
@app_commands.checks.has_permissions(manage_guild=True)
async def keypanel_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    panels = load_json("key_panels", {})
    guild_id = str(interaction.guild.id)
    guild_panels = panels.get(guild_id, {})

    if not guild_panels:
        await interaction.followup.send("ℹ️ No key panels found in this server.", ephemeral=True)
        return

    embed = discord.Embed(
        title="📋 Key Generator Panels",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )

    for msg_id, cfg in guild_panels.items():
        channel = interaction.guild.get_channel(cfg["channel_id"])
        ch_mention = channel.mention if channel else f"Deleted Channel ({cfg['channel_id']})"
        product = cfg.get("product_name") or "Premium Key"
        label = cfg.get("button_label") or "Generate Key"
        
        # Support both legacy and new configurations in listing
        required_role_ids = cfg.get("required_role_ids") or []
        legacy_role_id = cfg.get("required_role_id")
        if legacy_role_id and legacy_role_id not in required_role_ids:
            required_role_ids = list(required_role_ids) + [legacy_role_id]
            
        role_mentions_str = "None"
        if required_role_ids:
            mentions = []
            for rid in required_role_ids:
                role = interaction.guild.get_role(int(rid))
                if role:
                    mentions.append(role.mention)
                else:
                    mentions.append(f"Deleted Role ({rid})")
            role_mentions_str = ", ".join(mentions)
            
        embed.add_field(
            name=f"Message ID: {msg_id}",
            value=(
                f"**Channel:** {ch_mention}\n"
                f"**Product:** `{product}`\n"
                f"**Button Label:** `{label}`\n"
                f"**Required Roles:** {role_mentions_str}\n"
                f"**Title:** {cfg.get('title')}\n"
            ),
            inline=False
        )

    await interaction.followup.send(embed=embed, ephemeral=True)


@keypanel_group.command(name="delete", description="Delete a key generator panel")
@app_commands.describe(message_id="The message ID of the key panel to delete")
@app_commands.checks.has_permissions(manage_guild=True)
async def keypanel_delete(interaction: discord.Interaction, message_id: str):
    await interaction.response.defer(ephemeral=True)
    panels = load_json("key_panels", {})
    guild_id = str(interaction.guild.id)
    guild_panels = panels.get(guild_id, {})

    if message_id not in guild_panels:
        await interaction.followup.send(f"❌ Key panel with Message ID `{message_id}` not found.", ephemeral=True)
        return

    cfg = guild_panels[message_id]
    channel = interaction.guild.get_channel(cfg["channel_id"])
    if channel:
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.delete()
        except Exception:
            pass

    del guild_panels[message_id]
    panels[guild_id] = guild_panels
    save_json("key_panels", panels)

    await interaction.followup.send("🗑️ Key panel deleted successfully.", ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: commands.Bot):
    bot.tree.add_command(keypanel_group)
    bot.add_view(KeyPanelButtonView())
