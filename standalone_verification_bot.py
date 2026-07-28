"""
standalone_verification_bot.py
A super-lightweight, standalone Discord Verification Bot connected to your dashboard configs.
Reads server settings from database/onboarding_configs.json (configured on your website dashboard)
as well as local verification_bot_config.json.

Commands:
- /setup-verify : Post a persistent verification button embed in a channel.
- /verify       : Manually verify a member.
"""

import os
import json
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("TOKEN") or ""
BASE_DIR = Path(__file__).parent
ONBOARDING_FILE = BASE_DIR / "database" / "onboarding_configs.json"
LOCAL_CONFIG_FILE = BASE_DIR / "verification_bot_config.json"


def get_guild_verification_config(guild_id: str | int) -> dict:
    gid = str(guild_id)
    verified_role_id = None
    channel_id = None

    # 1. Try reading from website dashboard database (database/onboarding_configs.json)
    if ONBOARDING_FILE.exists():
        try:
            with open(ONBOARDING_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                guild_cfg = data.get(gid, {})
                if isinstance(guild_cfg, dict):
                    verified_role_id = guild_cfg.get("verified_role_id")
                    channel_id = guild_cfg.get("welcome_channel_id")
        except Exception:
            pass

    # 2. Fall back to / Overwrite with local verification_bot_config.json
    if LOCAL_CONFIG_FILE.exists():
        try:
            with open(LOCAL_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                local_cfg = data.get(gid, {})
                if isinstance(local_cfg, dict):
                    if local_cfg.get("verified_role_id"):
                        verified_role_id = local_cfg["verified_role_id"]
                    if local_cfg.get("channel_id"):
                        channel_id = local_cfg["channel_id"]
        except Exception:
            pass

    return {
        "verified_role_id": str(verified_role_id) if verified_role_id else None,
        "channel_id": str(channel_id) if channel_id else None,
    }


def save_local_config(guild_id: str | int, verified_role_id: str | int, channel_id: str | int):
    gid = str(guild_id)
    data = {}
    if LOCAL_CONFIG_FILE.exists():
        try:
            with open(LOCAL_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    data[gid] = {
        "verified_role_id": str(verified_role_id),
        "channel_id": str(channel_id),
    }

    try:
        with open(LOCAL_CONFIG_FILE, "w", encoding="utf-8") as f:
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

        cfg = get_guild_verification_config(interaction.guild.id)
        role_id = cfg.get("verified_role_id")

        if not role_id:
            await interaction.response.send_message("❌ No verified role has been configured on the dashboard or via `/setup-verify` yet.", ephemeral=True)
            return

        role = interaction.guild.get_role(int(role_id))
        if not role:
            await interaction.response.send_message(f"❌ Could not find verified role (ID: `{role_id}`). Please check server role settings.", ephemeral=True)
            return

        if role in interaction.user.roles:
            await interaction.response.send_message("ℹ️ You are already verified!", ephemeral=True)
            return

        try:
            await interaction.user.add_roles(role, reason="Verification button clicked")
            await interaction.response.send_message(f"✅ Success! You have been verified and given the **{role.name}** role.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Bot lacks permission to assign the verified role. Ensure the bot's role is placed HIGHER than the Verified Role.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error assigning role: {e}", ephemeral=True)


class VerificationBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
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

    save_local_config(interaction.guild.id, role.id, channel.id)

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
    embed.set_footer(
        text=f"{interaction.guild.name} Verification",
        icon_url=interaction.guild.icon.url if interaction.guild.icon else None
    )

    try:
        await channel.send(embed=embed, view=VerifyButtonView())
        await interaction.followup.send(
            f"✅ Verification panel sent to {channel.mention}! Users clicking the button will receive **{role.name}**.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to send panel to channel: {e}", ephemeral=True)


@bot.tree.command(name="verify", description="Manually verify a member")
@app_commands.describe(member="Member to verify")
@app_commands.checks.has_permissions(manage_roles=True)
async def verify_member(interaction: discord.Interaction, member: discord.Member):
    cfg = get_guild_verification_config(interaction.guild.id)
    role_id = cfg.get("verified_role_id")

    if not role_id:
        await interaction.response.send_message("❌ No verified role configured. Configure it on your website dashboard or run `/setup-verify`.", ephemeral=True)
        return

    role = interaction.guild.get_role(int(role_id))
    if not role:
        await interaction.response.send_message(f"❌ Verified role (ID: `{role_id}`) not found.", ephemeral=True)
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
