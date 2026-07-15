"""
modules/reaction_roles.py
Reaction roles – config, slash commands, raw reaction event handlers.
"""

from __future__ import annotations

import discord
from discord import app_commands

from modules.utils import load_json, save_json, REACTION_ROLES_FILE

_bot: discord.ext.commands.Bot | None = None


def load_reaction_roles() -> dict:
    return load_json(REACTION_ROLES_FILE, {})


def save_reaction_roles(data: dict):
    save_json(REACTION_ROLES_FILE, data)


# ── Slash commands ────────────────────────────────────────────────────────────

reaction_roles_group = app_commands.Group(name="reaction_roles", description="Manage reaction role messages")


@reaction_roles_group.command(name="create", description="Create a new reaction role message")
@app_commands.describe(title="Title of the embed", description="Description text", channel="Channel to send the message to")
async def reaction_roles_create(interaction: discord.Interaction, title: str, description: str, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ You need **Manage Roles** permission.", ephemeral=True)
        return
    embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    embed.set_footer(text="React with the emojis below to get roles!")
    message = await channel.send(embed=embed)
    reaction_roles = load_reaction_roles()
    reaction_roles[str(message.id)] = {
        "guild_id": interaction.guild.id,
        "channel_id": channel.id,
        "title": title,
        "description": description,
        "roles": {},
    }
    save_reaction_roles(reaction_roles)
    await interaction.response.send_message(
        f"✅ Created reaction role message in {channel.mention}!\nUse `/reaction_roles add_role` to add role options.",
        ephemeral=True,
    )


@reaction_roles_group.command(name="add_role", description="Add a role option to an existing reaction role message")
@app_commands.describe(message_id="ID of the reaction role message", emoji="Emoji for this role", role="Role to assign", label="Optional label")
async def reaction_roles_add_role(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role, label: str = None):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ You need **Manage Roles** permission.", ephemeral=True)
        return
    reaction_roles = load_reaction_roles()
    if message_id not in reaction_roles:
        await interaction.response.send_message("❌ Reaction role message not found.", ephemeral=True)
        return
    message_data = reaction_roles[message_id]
    if message_data["guild_id"] != interaction.guild.id:
        await interaction.response.send_message("❌ This message belongs to a different server.", ephemeral=True)
        return
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(f"❌ I cannot assign {role.mention} – it's higher than my top role.", ephemeral=True)
        return
    message_data["roles"][emoji] = {"role_id": role.id, "label": label or role.name}
    save_reaction_roles(reaction_roles)
    try:
        channel = _bot.get_channel(message_data["channel_id"])
        msg = await channel.fetch_message(int(message_id))
        embed = discord.Embed(title=message_data["title"], description=message_data["description"], color=discord.Color.blurple())
        role_text = "".join(f"{em} {info['label']}\n" for em, info in message_data["roles"].items())
        if role_text:
            embed.add_field(name="Available Roles", value=role_text, inline=False)
        embed.set_footer(text="React with the emojis below to get roles!")
        await msg.edit(embed=embed)
        await msg.add_reaction(emoji)
        await interaction.response.send_message(f"✅ Added {emoji} → {role.mention}!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to update message: {e}", ephemeral=True)


@reaction_roles_group.command(name="remove_role", description="Remove a role option from a reaction role message")
@app_commands.describe(message_id="ID of the reaction role message", emoji="Emoji to remove")
async def reaction_roles_remove_role(interaction: discord.Interaction, message_id: str, emoji: str):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ You need **Manage Roles** permission.", ephemeral=True)
        return
    reaction_roles = load_reaction_roles()
    if message_id not in reaction_roles:
        await interaction.response.send_message("❌ Reaction role message not found.", ephemeral=True)
        return
    message_data = reaction_roles[message_id]
    if message_data["guild_id"] != interaction.guild.id:
        await interaction.response.send_message("❌ This message belongs to a different server.", ephemeral=True)
        return
    if emoji not in message_data["roles"]:
        await interaction.response.send_message("❌ That emoji is not configured for this message.", ephemeral=True)
        return
    message_data["roles"].pop(emoji)
    save_reaction_roles(reaction_roles)
    try:
        channel = _bot.get_channel(message_data["channel_id"])
        msg = await channel.fetch_message(int(message_id))
        embed = discord.Embed(title=message_data["title"], description=message_data["description"], color=discord.Color.blurple())
        role_text = "".join(f"{em} {info['label']}\n" for em, info in message_data["roles"].items())
        if role_text:
            embed.add_field(name="Available Roles", value=role_text, inline=False)
        embed.set_footer(text="React with the emojis below to get roles!")
        await msg.edit(embed=embed)
        await msg.clear_reaction(emoji)
        await interaction.response.send_message(f"✅ Removed {emoji} from the reaction role message!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to update message: {e}", ephemeral=True)


@reaction_roles_group.command(name="list", description="List all reaction role messages in this server")
async def reaction_roles_list(interaction: discord.Interaction):
    reaction_roles = load_reaction_roles()
    guild_messages = [(mid, data) for mid, data in reaction_roles.items() if data["guild_id"] == interaction.guild.id]
    if not guild_messages:
        await interaction.response.send_message("❌ No reaction role messages found.", ephemeral=True)
        return
    embed = discord.Embed(title="Reaction Role Messages", color=discord.Color.blurple())
    for mid, data in guild_messages:
        embed.add_field(
            name=data["title"],
            value=f"Message ID: `{mid}`\nChannel: <#{data['channel_id']}>\nRoles: {len(data['roles'])}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Raw reaction event handlers ───────────────────────────────────────────────

async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == _bot.user.id:
        return
    reaction_roles = load_reaction_roles()
    message_data = reaction_roles.get(str(payload.message_id))
    if not message_data:
        return
    emoji_str = str(payload.emoji)
    role_info = message_data["roles"].get(emoji_str)
    if not role_info:
        return
    guild = _bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    if not member:
        return
    role = guild.get_role(role_info["role_id"])
    if not role:
        return
    try:
        await member.add_roles(role, reason="Reaction role")
    except Exception as e:
        print(f"[ReactionRoles] Failed to add role to {member}: {e}")


async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.user_id == _bot.user.id:
        return
    reaction_roles = load_reaction_roles()
    message_data = reaction_roles.get(str(payload.message_id))
    if not message_data:
        return
    emoji_str = str(payload.emoji)
    role_info = message_data["roles"].get(emoji_str)
    if not role_info:
        return
    guild = _bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    if not member:
        return
    role = guild.get_role(role_info["role_id"])
    if not role:
        return
    try:
        await member.remove_roles(role, reason="Reaction role removed")
    except Exception as e:
        print(f"[ReactionRoles] Failed to remove role from {member}: {e}")


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: discord.ext.commands.Bot):
    global _bot
    _bot = bot
    bot.tree.add_command(reaction_roles_group)
    bot.add_listener(on_raw_reaction_add, "on_raw_reaction_add")
    bot.add_listener(on_raw_reaction_remove, "on_raw_reaction_remove")


def build_reaction_role_embed(
    title: str,
    description: str,
    roles: dict | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blurple(),
    )

    if isinstance(roles, dict) and roles:
        role_lines: list[str] = []
        for emoji_str, role_info in roles.items():
            if isinstance(role_info, dict):
                label = str(role_info.get("label") or "").strip()
            else:
                label = ""
            if not label:
                continue
            role_lines.append(f"{emoji_str} {label}")

        if role_lines:
            embed.add_field(name="Available Roles", value="\n".join(role_lines), inline=False)

    embed.set_footer(text="React with the emojis below to get roles!")
    return embed


def resolve_trigger_channel(
    guild: discord.Guild,
    payload: dict,
    key: str = "channel_id",
) -> tuple[int | None, discord.abc.GuildChannel | None]:
    channel_id = _as_int(payload.get(key))
    if not channel_id:
        return None, None
    return channel_id, guild.get_channel(channel_id)

