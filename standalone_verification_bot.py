"""
standalone_verification_bot.py
A super-lightweight, standalone Discord Verification Bot with ZERO heavy dependencies.
Requires ONLY discord.py and python-dotenv.

Commands:
- /setup-verify : Post a persistent verification button embed in a channel.
- /verify       : Manually verify a member.
"""

import os
import json
import asyncio
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("TOKEN") or ""
CONFIG_FILE = Path(__file__).parent / "verification_bot_config.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(data: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[VerificationBot] Error saving config: {e}")


# Persistent Verification Button View
class VerifyButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify",
        style=discord.ButtonStyle.green,
        emoji="✅",
        custom_id="standalone_verify_button"
    )
    async def verify_button_click(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ This button can only be used in a server.", ephemeral=True)
            return

        cfg = load_config().get(str(interaction.guild.id), {})
        role_id = cfg.get("verified_role_id")

        if not role_id:
            await interaction.response.send_message("❌ No verified role has been configured for this server yet.", ephemeral=True)
            return

        role = interaction.guild.get_role(int(role_id))
        if not role:
            await interaction.response.send_message("❌ The configured verified role no longer exists.", ephemeral=True)
            return

        if role in interaction.user.roles:
            await interaction.response.send_message("ℹ️ You are already verified!", ephemeral=True)
            return

        try:
            await interaction.user.add_roles(role, reason="Verification button clicked")
            await interaction.response.send_message(f"✅ Success! You have been verified and given the **{role.name}** role.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Bot lacks permission to assign the verified role. Please check role hierarchy.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error assigning role: {e}", ephemeral=True)


class VerificationBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Register persistent view so button works across bot restarts
        self.add_view(VerifyButtonView())
        print("[VerificationBot] Persistent verification view registered.")

    async def on_ready(self):
        print(f"[VerificationBot] Logged in as {self.user} (ID: {self.user.id})")
        try:
            synced = await self.tree.sync()
            print(f"[VerificationBot] Synced {len(synced)} slash commands.")
        except Exception as e:
            print(f"[VerificationBot] Command sync error: {e}")


bot = VerificationBot()


@bot.tree.command(name="setup-verify", description="Set up a persistent verification button in a channel")
@app_commands.describe(
    channel="Channel to post the verification panel",
    role="Role to grant when users verify",
    title="Custom embed title (optional)",
    description="Custom embed description (optional)",
    color_hex="Hex color e.g. #00FF00 (optional)"
)
@app_commands.checks.has_permissions(manage_roles=True)
async def setup_verify(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    role: discord.Role,
    title: Optional[str] = None,
    description: Optional[str] = None,
    color_hex: Optional[str] = None
):
    await interaction.response.defer(ephemeral=True)

    guild_id = str(interaction.guild.id)
    cfg = load_config()
    cfg[guild_id] = {
        "verified_role_id": role.id,
        "channel_id": channel.id,
    }
    save_config(cfg)

    # Parse color
    color = discord.Color.green()
    if color_hex:
        try:
            color = discord.Color(int(color_hex.lstrip("#"), 16))
        except ValueError:
            pass

    embed = discord.Embed(
        title=title or "🔒 Verification Required",
        description=description or "Click the button below to verify yourself and gain full access to the server!",
        color=color
    )
    embed.set_footer(text=f"{interaction.guild.name} Verification", icon_url=interaction.guild.icon.url if interaction.guild.icon else None)

    try:
        await channel.send(embed=embed, view=VerifyButtonView())
        await interaction.followup.send(f"✅ Verification panel sent to {channel.mention}! Users clicking the button will receive **{role.name}**.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to send panel to channel: {e}", ephemeral=True)


@bot.tree.command(name="verify", description="Manually verify a member")
@app_commands.describe(member="Member to verify")
@app_commands.checks.has_permissions(manage_roles=True)
async def verify_member(interaction: discord.Interaction, member: discord.Member):
    guild_id = str(interaction.guild.id)
    cfg = load_config()
    role_id = cfg.get(guild_id, {}).get("verified_role_id")

    if not role_id:
        await interaction.response.send_message("❌ No verified role configured. Run `/setup-verify` first.", ephemeral=True)
        return

    role = interaction.guild.get_role(int(role_id))
    if not role:
        await interaction.response.send_message("❌ Verified role not found.", ephemeral=True)
        return

    try:
        await member.add_roles(role, reason=f"Manually verified by {interaction.user}")
        await interaction.response.send_message(f"✅ Successfully verified {member.mention} and assigned **{role.name}**.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to assign role: {e}", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        print("[VerificationBot] ERROR: No DISCORD_BOT_TOKEN found in .env or environment!")
    else:
        bot.run(TOKEN)
