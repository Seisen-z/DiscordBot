"""
modules/boost.py
Server boost system – config, ClaimKeyView, event handler, slash commands.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os

import aiohttp
import discord
from discord import app_commands

from modules.utils import load_json, save_json, BOOST_CONFIG_FILE

# In-memory store: { guild_id: { user_id: "KEY_HERE" } }
_unclaimed_keys: dict[int, dict[int, str]] = {}

BOOST_REWARD_ROLE_ID = 1402982327935832104

def _parse_id(val: any) -> int | None:
    try:
        return int(str(val or "").strip())
    except (ValueError, TypeError):
        return None

# OFFICIAL_GUILD_ID removed - configuration is now guild-agnostic via dashboard.


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_boost_config() -> dict:
    return load_json(BOOST_CONFIG_FILE, {})


def save_boost_config(data: dict):
    save_json(BOOST_CONFIG_FILE, data)


# ── View ──────────────────────────────────────────────────────────────────────

class CloseTicketView(discord.ui.View):
    """
    Persistent view (timeout=None + static custom_id) registered once via
    bot.add_view() in register(). Ownership can't be stored on the instance
    because the same registered instance handles clicks for every boost
    channel ever created, including ones from before the last restart — it
    must be looked up per-channel from the boost config instead.
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="boost:close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = load_boost_config()
        guild_id = str(interaction.guild_id)
        gcfg = cfg.get(guild_id, {})
        open_tickets = gcfg.get("open_boost_tickets", {})
        owner_id = open_tickets.get(str(interaction.channel_id))
        is_owner = owner_id is not None and int(owner_id) == interaction.user.id
        is_staff = interaction.user.guild_permissions.manage_channels

        if not (is_owner or is_staff):
            await interaction.response.send_message("❌ Only the booster or staff can close this ticket!", ephemeral=True)
            return

        await interaction.response.send_message("🔒 Closing ticket in 3 seconds...")

        if str(interaction.channel_id) in open_tickets:
            del open_tickets[str(interaction.channel_id)]
            gcfg["open_boost_tickets"] = open_tickets
            cfg[guild_id] = gcfg
            save_boost_config(cfg)

        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason="Boost ticket closed.")
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to delete channel: {e}", ephemeral=True)


# ── Webhook key helper ────────────────────────────────────────────────────────

async def _request_boost_key(webhook_url: str, webhook_secret: str | None) -> str | None:
    payload = {"item": {"product": {"name": "Premium Key"}, "quantity": 1}}
    payload_bytes = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if webhook_secret:
        sig = hmac.new(webhook_secret.encode(), msg=payload_bytes, digestmod=hashlib.sha256).hexdigest()
        for k in ["X-Signature", "x-jnkie-signature", "signature", "jnkie-signature",
                  "Authorization", "X-Hub-Signature", "X-Hub-Signature-256",
                  "Discord-Boosting", "X-Discord-Boosting", "Discord-Boosting-Signature", "DiscordBoosting"]:
            headers[k] = sig
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload, headers=headers) as resp:
                text = await resp.text()
                if resp.status == 200:
                    try:
                        data = json.loads(text)
                        if "keys" in data and data["keys"]:
                            return data["keys"][0]
                        if "key" in data:
                            return data["key"]
                    except json.JSONDecodeError:
                        if not text.strip().startswith("<") and text.strip():
                            return text.strip()
                else:
                    print(f"[Boost] Webhook failed with status {resp.status}: {text}")
    except Exception as e:
        print(f"[Boost] Exception generating key via webhook: {e}")
    return None


# ── on_member_update handler ─────────────────────────────────────────────────

async def deliver_boost_reward(guild: discord.Guild, member: discord.Member, is_test: bool = False):
    """Processes the boost reward: roles, webhook key generation, and ticket creation."""
    boost_role = guild.get_role(BOOST_REWARD_ROLE_ID)
    if boost_role and boost_role not in member.roles:
        try:
            await member.add_roles(boost_role, reason="Started boosting the server (or test)")
        except Exception as e:
            print(f"[Boost] Failed to add boost role to {member.display_name}: {e}")

    webhook_url = os.getenv("WEBHOOK_URL")
    webhook_secret = os.getenv("WEBHOOK_HMAC_SECRET")

    generated_key = None
    # Key delivery is now available to all configured servers.
    if webhook_url:
        generated_key = await _request_boost_key(webhook_url, webhook_secret)

    if generated_key:
        if guild.id not in _unclaimed_keys:
            _unclaimed_keys[guild.id] = {}
        _unclaimed_keys[guild.id][member.id] = generated_key

    cfg = load_boost_config()
    guild_id_str = str(guild.id)
    # Strict check: only proceed if the guild has explicitly saved a boost configuration.
    if guild_id_str not in cfg:
        print(f"[Boost] Skipping {guild.name} ({guild.id}) - no configuration found.")
        return

    guild_cfg = cfg.get(guild_id_str, {})
    channel_id = int(guild_cfg["channel_id"]) if guild_cfg.get("channel_id") else None
    
    if not channel_id:
        print(f"[Boost] Skipping {guild.name} ({guild.id}) - no log channel configured.")
        return

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except:
            print(f"[Boost] Could not find configured channel {channel_id} in {guild.name}.")
            return

    allowed_roles = [int(r) for r in guild_cfg.get("roles", [])]
    category_id = int(guild_cfg["category_id"]) if guild_cfg.get("category_id") else None

    view = CloseTicketView()

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True, send_messages=True),
    }
    for rid in allowed_roles:
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    category = guild.get_channel(category_id) if category_id else None
    ticket_channel = None
    try:
        ticket_prefix = "boost-claim-test" if is_test else "boost-claim"
        ticket_channel = await guild.create_text_channel(
            name=f"{ticket_prefix}-{member.name}",
            category=category,
            overwrites=overwrites,
            reason=f"Boost claim channel for {member.name}",
        )
        guild_cfg.setdefault("open_boost_tickets", {})[str(ticket_channel.id)] = str(member.id)
        cfg[guild_id_str] = guild_cfg
        save_boost_config(cfg)
    except Exception as e:
        print(f"[Boost] Failed to create ticket channel: {e}")

    # Prepare the log embed for the configured log channel
    log_embed = discord.Embed(
        title="🚀 New Server Boost!" + (" (TEST)" if is_test else ""),
        description=(
            f"💗 Thank you {member.mention} for boosting the server!\n\n"
            f"**Premium Key:**\n```{generated_key if generated_key else 'None currently available. Please contact an admin.'}```\n\n"
            f"[Redeem your key here!](https://discord.com/channels/{guild.id}/1421560929425817662)"
        ),
        color=discord.Color.from_str("#f47fff"),
        timestamp=discord.utils.utcnow()
    )
    log_embed.set_thumbnail(url=member.display_avatar.url)
    log_embed.set_footer(text=f"{guild.name} • Boost count: {guild.premium_subscription_count}")

    if ticket_channel:
        welcome_embed = discord.Embed(
            title="🎁 Boost Reward Ticket" + (" (TEST)" if is_test else ""),
            description=(
                f"Hello {member.mention}! Thank you so much for boosting **{guild.name}**!\n\n"
                f"**Your Premium Key:**\n```{generated_key if generated_key else 'None currently available. Please contact an admin.'}```\n\n"
                "[Redeem your key here!](https://discord.com/channels/1333251917098520628/1421560929425817662)\n\n"
                "Once you're done, you can click **Close Ticket** to delete this channel."
            ),
            color=discord.Color.from_str("#f47fff"),
        )
        welcome_embed.set_thumbnail(url=member.display_avatar.url)
        
        mentions = " ".join(f"<@&{rid}>" for rid in allowed_roles) if allowed_roles else ""
        try:
            await ticket_channel.send(content=f"{member.mention} {mentions}", embed=welcome_embed, view=view)
            log_embed.add_field(name="Delivery Status", value=f"✅ Key sent in ticket: {ticket_channel.mention}", inline=False)
        except Exception as e:
            print(f"[Boost] Failed to send to ticket channel: {e}")
            log_embed.add_field(name="Delivery Status", value="⚠️ Failed to send to ticket channel", inline=False)
    else:
        if generated_key:
            log_embed.add_field(name="Delivery Status", value="✅ Key provided above!", inline=False)
        else:
            log_embed.add_field(name="Delivery Status", value="❌ Failed to generate key", inline=False)

    # Send the log message to the configured channel
    mentions = " ".join(f"<@&{rid}>" for rid in allowed_roles) if allowed_roles else ""
    try:
        await channel.send(content=f"{member.mention} {mentions}", embed=log_embed)
    except Exception as e:
        print(f"[Boost] Failed to send log message: {e}")

    return generated_key

async def handle_member_update(before: discord.Member, after: discord.Member):
    """Called from main.py's on_member_update event."""

    # Boost started
    if before.premium_since is None and after.premium_since is not None:
        await deliver_boost_reward(after.guild, after, is_test=False)

    # Boost expired
    elif before.premium_since is not None and after.premium_since is None:
        guild = after.guild
        boost_role = guild.get_role(BOOST_REWARD_ROLE_ID)
        if boost_role and boost_role in after.roles:
            try:
                await after.remove_roles(boost_role, reason="Stopped boosting the server")
            except Exception as e:
                print(f"[Boost] Failed to remove boost role from {after.display_name}: {e}")


# ── Slash commands ────────────────────────────────────────────────────────────

boost_group = app_commands.Group(name="boost", description="Server boost configuration commands")


@boost_group.command(name="setlog", description="Set the channel where server boost keys will be logged")
@app_commands.describe(channel="The text channel to send logs to")
async def boost_setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = load_boost_config()
    guild_id = str(interaction.guild_id)
    cfg.setdefault(guild_id, {})["channel_id"] = str(channel.id)
    save_boost_config(cfg)
    await interaction.response.send_message(f"✅ Boost logs will now be sent to {channel.mention}", ephemeral=True)


@boost_group.command(name="addrole", description="Add roles to be mentioned when a boost key is logged (up to 5)")
@app_commands.describe(
    role1="1st role to mention", role2="2nd role (optional)", role3="3rd role (optional)",
    role4="4th role (optional)", role5="5th role (optional)",
)
async def boost_addrole(
    interaction: discord.Interaction,
    role1: discord.Role,
    role2: discord.Role = None,
    role3: discord.Role = None,
    role4: discord.Role = None,
    role5: discord.Role = None,
):
    cfg = load_boost_config()
    guild_id = str(interaction.guild_id)
    cfg.setdefault(guild_id, {})
    roles = [str(r) for r in cfg[guild_id].get("roles", [])]
    added, already = [], []
    for r in [role1, role2, role3, role4, role5]:
        if r:
            if str(r.id) not in roles:
                roles.append(str(r.id))
                added.append(r.mention)
            else:
                already.append(r.mention)
    if added:
        cfg[guild_id]["roles"] = roles
        save_boost_config(cfg)
        msg = f"✅ Added {', '.join(added)} to boost notifications."
        if already:
            msg += f"\nℹ️ {', '.join(already)} were already set."
        await interaction.response.send_message(msg, ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ {', '.join(already)} are already set.", ephemeral=True)


@boost_group.command(name="removerole", description="Remove a role from being mentioned on server boosts")
@app_commands.describe(role="The role to remove")
async def boost_removerole(interaction: discord.Interaction, role: discord.Role):
    cfg = load_boost_config()
    guild_id = str(interaction.guild_id)
    if guild_id not in cfg:
        await interaction.response.send_message("❌ No configuration found for this guild.", ephemeral=True)
        return
    roles = [str(r) for r in cfg[guild_id].get("roles", [])]
    if str(role.id) in roles:
        roles.remove(str(role.id))
        cfg[guild_id]["roles"] = roles
        save_boost_config(cfg)
        await interaction.response.send_message(f"✅ Role {role.name} removed from boost notifications.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Role {role.name} is not in the notification list.", ephemeral=True)


@boost_group.command(name="category", description="Set the category where boost claim channels will be created")
@app_commands.describe(category="The category to create claim channels in")
async def boost_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    cfg = load_boost_config()
    guild_id = str(interaction.guild_id)
    cfg.setdefault(guild_id, {})["category_id"] = str(category.id)
    save_boost_config(cfg)
    await interaction.response.send_message(f"✅ Boost claim channels will now be created in **{category.name}**", ephemeral=True)


@boost_group.command(name="config", description="Show the current server boost configuration")
async def boost_config_show(interaction: discord.Interaction):
    cfg = load_boost_config()
    gcfg = cfg.get(str(interaction.guild_id), {})
    channel = interaction.guild.get_channel(int(gcfg.get("channel_id"))) if gcfg.get("channel_id") else None
    category = interaction.guild.get_channel(int(gcfg.get("category_id"))) if gcfg.get("category_id") else None
    roles = [r.mention for r in [interaction.guild.get_role(int(rid)) for rid in gcfg.get("roles", [])] if r]
    embed = discord.Embed(title="🚀 Boost Configuration", color=discord.Color.from_str("#f47fff"))
    embed.add_field(name="Log Channel", value=channel.mention if channel else "None (Falls back to System Channel)", inline=False)
    embed.add_field(name="Ticket Category", value=category.name if category else "None (Created at top)", inline=False)
    embed.add_field(name="Notification Roles", value=", ".join(roles) if roles else "None", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@boost_group.command(name="test", description="Test the server boost webhook and logging system")
async def boost_test(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        await interaction.followup.send("❌ No WEBHOOK_URL configured in environment.", ephemeral=True)
        return
    await deliver_boost_reward(interaction.guild, interaction.user, is_test=True)
    await interaction.followup.send(f"✅ Webhook test completed! Check your server channels.", ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: discord.ext.commands.Bot):
    bot.tree.add_command(boost_group)
    bot.add_listener(handle_member_update, 'on_member_update')
    bot.add_view(CloseTicketView())
