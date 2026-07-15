"""
modules/access_control.py
Owner-lock access control + role management commands.
"""

from __future__ import annotations

import asyncio
import os

import discord
from discord import app_commands

from modules.utils import (
    load_json,
    save_json,
    COMMAND_ACCESS_FILE,
    parse_id_set_from_env,
    is_server_owner,
    normalize_command_name,
)

# ── Constants ─────────────────────────────────────────────────────────────────

OWNER_LOCKED_COMMANDS = {
    "announce", "announce_draft save", "announce_draft list", "announce_draft delete",
    "command",
    "activity leaderboard", "activity me", "activity test",
    "clear", "kick", "ban", "unban", "banlist", "baninfo", "purge",
    "invitelist", "role add", "role remove", "role all",
    "autoreply add", "autoreply edit", "autoreply setdelay",
    "autoreply edittargets", "autoreply remove", "autoreply list",
    "ticket setup", "ticket close",
    "roblox setup", "roblox remove", "roblox status", "roblox list", "roblox test",
    "sticky remove", "sticky message",
    "boost setlog", "boost addrole", "boost removerole", "boost test",
    "vouch_setup", "vouch",
    "poll create", "poll end",
    "giveaway create", "giveaway end", "giveaway reroll", "giveaway delete", "giveaway list",
    "membercounter create", "membercounter set", "membercounter sync",
    "membercounter disable", "membercounter status",
    "generatekeypremium", "generatekeyregular",
}

ENV_OWNER_LOCK_BYPASS_GUILD_IDS = parse_id_set_from_env("OWNER_LOCK_BYPASS_GUILD_IDS")
ENV_OWNER_LOCK_BYPASS_USER_IDS = parse_id_set_from_env("OWNER_LOCK_BYPASS_USER_IDS")
ENV_OWNER_LOCK_BYPASS_CHANNEL_IDS = parse_id_set_from_env("OWNER_LOCK_BYPASS_CHANNEL_IDS")


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_command_access() -> dict:
    return load_json(COMMAND_ACCESS_FILE, {})


def save_command_access(data: dict):
    save_json(COMMAND_ACCESS_FILE, data)


def _migrate_to_nested_schema(access: dict, guild_id_str: str) -> dict:
    """Ensure access[guild_id_str] uses the nested {"commands": {...}} schema
    the dashboard (api.py get_commands) expects, migrating legacy flat data
    (guild_id_str -> {command: [role_ids]}) in place if found."""
    guild_data = access.setdefault(guild_id_str, {})
    if "commands" not in guild_data and guild_data:
        old_data = dict(guild_data)
        guild_data = {"commands": old_data}
        access[guild_id_str] = guild_data
    guild_data.setdefault("commands", {})
    return guild_data


def is_owner_lock_bypassed(interaction: discord.Interaction) -> bool:
    if interaction.guild_id and interaction.guild_id in ENV_OWNER_LOCK_BYPASS_GUILD_IDS:
        return True
    if interaction.user.id in ENV_OWNER_LOCK_BYPASS_USER_IDS:
        return True
    channel_id = interaction.channel_id
    if channel_id and channel_id in ENV_OWNER_LOCK_BYPASS_CHANNEL_IDS:
        return True
    return False


# ── Command groups ────────────────────────────────────────────────────────────

role_group = app_commands.Group(name="role", description="Role management commands")
access_group = app_commands.Group(name="access", description="Owner-only access control for locked commands")


async def owner_locked_command_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    needle = current.lower().strip()
    matches = [cmd for cmd in sorted(OWNER_LOCKED_COMMANDS) if needle in cmd][:25]
    return [app_commands.Choice(name=f"/{cmd}", value=cmd) for cmd in matches]


# ── /access commands ─────────────────────────────────────────────────────────

@access_group.command(name="role", description="Allow a role to use a locked command")
@app_commands.describe(command="Locked command name (example: ban, kick, announce)", role="Role to allow")
@app_commands.autocomplete(command=owner_locked_command_autocomplete)
async def access_role(interaction: discord.Interaction, command: str, role: discord.Role):
    if not is_server_owner(interaction):
        await interaction.response.send_message("❌ Only the server owner can use this command.", ephemeral=True)
        return
    command_name = normalize_command_name(command)
    if command_name not in OWNER_LOCKED_COMMANDS:
        await interaction.response.send_message(
            "❌ That command is not in the locked command list. Use autocomplete to select a valid command.", ephemeral=True
        )
        return
    access = load_command_access()
    guild_id_str = str(interaction.guild.id)
    guild_data = _migrate_to_nested_schema(access, guild_id_str)
    cmd_access = guild_data["commands"]
    role_ids = cmd_access.setdefault(command_name, [])
    if role.id in role_ids:
        await interaction.response.send_message(
            f"ℹ️ {role.mention} is already allowed to use `/{command_name}`.", ephemeral=True
        )
        return
    role_ids.append(role.id)
    save_command_access(access)
    await interaction.response.send_message(f"✅ {role.mention} can now use `/{command_name}`.", ephemeral=True)


@access_group.command(name="remove", description="Remove a role from a locked command")
@app_commands.describe(command="Locked command name", role="Role to remove")
@app_commands.autocomplete(command=owner_locked_command_autocomplete)
async def access_remove(interaction: discord.Interaction, command: str, role: discord.Role):
    if not is_server_owner(interaction):
        await interaction.response.send_message("❌ Only the server owner can use this command.", ephemeral=True)
        return
    command_name = normalize_command_name(command)
    if command_name not in OWNER_LOCKED_COMMANDS:
        await interaction.response.send_message(
            "❌ That command is not in the locked command list.", ephemeral=True
        )
        return
    access = load_command_access()
    guild_id = str(interaction.guild.id)
    guild_data = _migrate_to_nested_schema(access, guild_id)
    cmd_access = guild_data["commands"]

    role_ids = cmd_access.get(command_name, [])
    if role.id not in role_ids:
        await interaction.response.send_message(
            f"ℹ️ {role.mention} is not currently allowed to use `/{command_name}`.", ephemeral=True
        )
        return
    role_ids.remove(role.id)
    save_command_access(access)
    await interaction.response.send_message(f"✅ Removed {role.mention} from `/{command_name}` access.", ephemeral=True)


@access_group.command(name="lock", description="Lock a command back to owner-only")
@app_commands.describe(command="Locked command name")
@app_commands.autocomplete(command=owner_locked_command_autocomplete)
async def access_lock(interaction: discord.Interaction, command: str):
    if not is_server_owner(interaction):
        await interaction.response.send_message("❌ Only the server owner can use this command.", ephemeral=True)
        return
    command_name = normalize_command_name(command)
    if command_name not in OWNER_LOCKED_COMMANDS:
        await interaction.response.send_message(
            "❌ That command is not in the locked command list.", ephemeral=True
        )
        return
    access = load_command_access()
    guild_id = str(interaction.guild.id)
    guild_access = access.get(guild_id, {})
    cmd_access = guild_access.get("commands", guild_access) if isinstance(guild_access, dict) else {}
    cmd_access.pop(command_name, None)
    if "commands" in guild_access and not cmd_access:
        guild_access.pop("commands", None)
    if guild_access:
        access[guild_id] = guild_access
    else:
        access.pop(guild_id, None)
    save_command_access(access)
    await interaction.response.send_message(f"🔒 `/{command_name}` is now owner-only.", ephemeral=True)


@access_group.command(name="list", description="List role access for locked commands")
@app_commands.describe(command="Optional: show only one locked command")
@app_commands.autocomplete(command=owner_locked_command_autocomplete)
async def access_list(interaction: discord.Interaction, command: str = None):
    if not is_server_owner(interaction):
        await interaction.response.send_message("❌ Only the server owner can use this command.", ephemeral=True)
        return
    command_name = normalize_command_name(command) if command else None
    if command_name and command_name not in OWNER_LOCKED_COMMANDS:
        await interaction.response.send_message(
            "❌ That command is not in the locked command list.", ephemeral=True
        )
        return
    access_all = load_command_access()
    guild_data = access_all.get(str(interaction.guild.id), {})
    access = guild_data.get("commands", guild_data)
    commands_to_show = [command_name] if command_name else sorted(OWNER_LOCKED_COMMANDS)
    lines = []
    for cmd in commands_to_show:
        role_ids = access.get(cmd, [])
        if not role_ids:
            lines.append(f"• `/{cmd}` → **Owner only**")
            continue
        mentions = []
        for rid in role_ids:
            role_obj = interaction.guild.get_role(rid)
            mentions.append(role_obj.mention if role_obj else f"(deleted role: {rid})")
        lines.append(f"• `/{cmd}` → {', '.join(mentions)}")
    embed = discord.Embed(
        title="🔐 Locked Command Access",
        description="\n".join(lines[:40]),
        color=discord.Color.blurple(),
    )
    if len(lines) > 40:
        embed.set_footer(text=f"Showing 40/{len(lines)} entries. Use command filter to narrow results.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /role commands ────────────────────────────────────────────────────────────

@role_group.command(name="add", description="Add a role to a member")
@app_commands.describe(member="The member to give the role to", role="The role to assign")
@app_commands.checks.has_permissions(manage_roles=True)
async def role_add(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ That role is higher than or equal to my highest role.", ephemeral=True)
        return
    if role in member.roles:
        await interaction.response.send_message(f"❌ {member.mention} already has {role.mention}.", ephemeral=True)
        return
    await member.add_roles(role, reason=f"Role added by {interaction.user}")
    await interaction.response.send_message(f"✅ Added {role.mention} to {member.mention}.", ephemeral=True)


@role_group.command(name="remove", description="Remove a role from a member")
@app_commands.describe(member="The member to remove the role from", role="The role to remove")
@app_commands.checks.has_permissions(manage_roles=True)
async def role_remove(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ That role is higher than or equal to my highest role.", ephemeral=True)
        return
    if role not in member.roles:
        await interaction.response.send_message(f"❌ {member.mention} doesn't have {role.mention}.", ephemeral=True)
        return
    await member.remove_roles(role, reason=f"Role removed by {interaction.user}")
    await interaction.response.send_message(f"✅ Removed {role.mention} from {member.mention}.", ephemeral=True)


@role_group.command(name="all", description="Give a role to every member in the server")
@app_commands.describe(role="The role to assign to all members")
@app_commands.checks.has_permissions(manage_roles=True)
async def role_all(interaction: discord.Interaction, role: discord.Role):
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ That role is higher than or equal to my highest role.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    members = [m for m in interaction.guild.members if not m.bot and role not in m.roles]
    if not members:
        await interaction.followup.send(f"❌ All members already have {role.mention}.", ephemeral=True)
        return
    await interaction.followup.send(
        f"⏳ Assigning {role.mention} to **{len(members)}** member(s)... This may take a while. I am doing this slowly to prevent Discord rate limits.",
        ephemeral=True,
    )
    reason = f"Mass role by {interaction.user}"
    failed = 0

    # Process sequentially with a 1.2s sleep between each to respect Discord's ~10 req/10 sec role endpoint limit
    for i, m in enumerate(members):
        try:
            await m.add_roles(role, reason=reason)
        except Exception:
            failed += 1
            
        await asyncio.sleep(1.2)

    success = len(members) - failed
    msg = f"✅ Done! Added {role.mention} to **{success}** member(s)."
    if failed:
        msg += f" Failed for **{failed}** member(s) (likely due to role hierarchy)."
    try:
        await interaction.followup.send(msg, ephemeral=True)
    except Exception:
        await interaction.channel.send(f"{interaction.user.mention} {msg}")


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: discord.ext.commands.Bot):
    bot.tree.add_command(role_group)
    bot.tree.add_command(access_group)
    bot.tree.interaction_check = owner_or_allowed_for_locked_commands


def get_allowed_role_ids(guild_id: int, command_name: str) -> list[int]:
    access = load_command_access()
    guild_access = access.get(str(guild_id), {})
    cmd_access = guild_access.get("commands", guild_access) if isinstance(guild_access, dict) else {}
    raw = cmd_access.get(normalize_command_name(command_name), [])
    result = []
    for r in raw:
        try:
            result.append(int(r))
        except (TypeError, ValueError):
            pass
    return result

async def send_ephemeral_error(interaction: discord.Interaction, message: str):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)

async def owner_or_allowed_for_locked_commands(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not interaction.command:
        return True

    if is_owner_lock_bypassed(interaction):
        return True

    command_name = normalize_command_name(interaction.command.qualified_name)
    if command_name not in OWNER_LOCKED_COMMANDS:
        return True

    if is_server_owner(interaction):
        return True

    allowed_role_ids = set(get_allowed_role_ids(interaction.guild.id, command_name))
    if allowed_role_ids and isinstance(interaction.user, discord.Member):
        user_role_ids = {role.id for role in interaction.user.roles}
        if user_role_ids & allowed_role_ids:
            return True

    await send_ephemeral_error(
        interaction,
        "❌ This command is locked to the server owner. Ask the owner to allow your role with `/access role`.",
    )
    return False

def bind_owner_locks_to_commands(bot):
    for cmd in bot.tree.walk_commands():
        command_name = normalize_command_name(cmd.qualified_name)
        if command_name not in OWNER_LOCKED_COMMANDS:
            continue

        extras = getattr(cmd, "extras", None)
        if extras is None:
            cmd.extras = {}
            extras = cmd.extras

        if extras.get("owner_lock_bound"):
            continue

        cmd.add_check(owner_or_allowed_for_locked_commands)
        extras["owner_lock_bound"] = True
