"""
standalone_verification_bot.py
A complete, ultra-lightweight Verification & Onboarding Discord Bot with ZERO heavy dependencies.
Requires ONLY discord.py and python-dotenv.

Runs completely independently from your main bot so Verification and Join Protection
stay 100% online 24/7 even if main servers restart.

Features:
- /setup-verify : Create a persistent verification button in a channel.
- /verify       : Manually verify a member.
- Persistent Button Click : Grants verified role instantly.
- Join Guard : Blocks alt accounts (min account age check, default avatar check).
- Auto Roles : Grants default member roles on join.
- Welcome Embed : Welcomes new members.
"""

import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
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
    default_cfg = {
        "verified_role_id": None,
        "auto_role_ids": [],
        "welcome_enabled": True,
        "welcome_channel_id": None,
        "welcome_content": "",
        "welcome_embed_title": "Welcome ${userglobalnickname}!",
        "welcome_embed_description": "to ${guildname}\n\nYou are member #${guildmembercount}.",
        "join_guard_enabled": False,
        "min_account_age_days": 0,
        "block_default_avatar": False,
        "join_guard_action": "kick",
        "join_guard_log_channel_id": None,
    }

    # Read from website dashboard database if present
    if ONBOARDING_FILE.exists():
        try:
            with open(ONBOARDING_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                guild_cfg = data.get(gid, {})
                if isinstance(guild_cfg, dict):
                    default_cfg.update(guild_cfg)
        except Exception:
            pass

    # Fallback to local config file
    if LOCAL_CONFIG_FILE.exists():
        try:
            with open(LOCAL_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                local_cfg = data.get(gid, {})
                if isinstance(local_cfg, dict):
                    if local_cfg.get("verified_role_id"):
                        default_cfg["verified_role_id"] = local_cfg["verified_role_id"]
                    if local_cfg.get("channel_id"):
                        default_cfg["welcome_channel_id"] = local_cfg["channel_id"]
        except Exception:
            pass

    return default_cfg


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
            await interaction.response.send_message("❌ Bot lacks permission to assign the verified role. Ensure the bot's role is placed HIGHER than the Verified Role in server settings.", ephemeral=True)
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

    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        cfg = get_guild_verification_config(member.guild.id)

        # 1. Run Join Guard
        if cfg.get("join_guard_enabled", False):
            reasons = []
            min_days = max(0, int(cfg.get("min_account_age_days") or 0))
            if min_days > 0:
                age = datetime.now(timezone.utc) - member.created_at
                if age < timedelta(days=min_days):
                    reasons.append(f"Account is too new ({age.days}d old, minimum required is {min_days}d).")

            if cfg.get("block_default_avatar", False) and member.avatar is None:
                reasons.append("Account has no custom avatar.")

            if reasons:
                action = str(cfg.get("join_guard_action") or "kick").lower()
                reason_text = "Join Guard: " + " | ".join(reasons)
                try:
                    await member.send(f" You were removed from **{member.guild.name}** by Join Guard.\nReason: {reason_text}")
                except Exception:
                    pass

                try:
                    if action == "ban":
                        await member.guild.ban(member, reason=reason_text, delete_message_days=0)
                    else:
                        await member.guild.kick(member, reason=reason_text)
                except Exception as e:
                    print(f"[VerificationBot] Join Guard kick/ban failed: {e}")

                log_ch_id = cfg.get("join_guard_log_channel_id")
                if log_ch_id:
                    log_ch = member.guild.get_channel(int(log_ch_id))
                    if log_ch and hasattr(log_ch, "send"):
                        try:
                            embed = discord.Embed(
                                title="🛡️ Join Guard Triggered",
                                description=f"{member.mention} was removed by join guard.",
                                color=0xED4245,
                                timestamp=datetime.now(timezone.utc),
                            )
                            embed.add_field(name="Action", value=action.upper(), inline=True)
                            embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)
                            embed.add_field(name="Reason", value=reason_text[:1024], inline=False)
                            await log_ch.send(embed=embed)
                        except Exception:
                            pass
                return  # Blocked, stop further join logic

        # 2. Assign Auto Roles
        auto_roles = cfg.get("auto_role_ids", [])
        if auto_roles:
            roles_to_add = []
            for rid in auto_roles:
                try:
                    role = member.guild.get_role(int(rid))
                    if role and role not in member.roles:
                        roles_to_add.append(role)
                except (ValueError, TypeError):
                    pass
            if roles_to_add:
                try:
                    await member.add_roles(*roles_to_add, reason="Auto-role on join")
                except Exception as e:
                    print(f"[VerificationBot] Failed to assign auto roles: {e}")

        # 3. Send Welcome Message / Embed if enabled
        if cfg.get("welcome_enabled", True):
            w_ch_id = cfg.get("welcome_channel_id")
            if w_ch_id:
                try:
                    w_ch = member.guild.get_channel(int(w_ch_id))
                    if w_ch and hasattr(w_ch, "send"):
                        desc = str(cfg.get("welcome_embed_description") or "Welcome to the server!")
                        desc = desc.replace("${userglobalnickname}", member.display_name)
                        desc = desc.replace("${guildname}", member.guild.name)
                        desc = desc.replace("${guildmembercount}", str(member.guild.member_count))

                        embed = discord.Embed(
                            title=f"Welcome {member.display_name}!",
                            description=desc,
                            color=0x5865F2,
                            timestamp=datetime.now(timezone.utc)
                        )
                        embed.set_thumbnail(url=member.display_avatar.url)
                        await w_ch.send(content=f"Welcome {member.mention}!", embed=embed)
                except Exception as e:
                    print(f"[VerificationBot] Failed to send welcome message: {e}")


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
