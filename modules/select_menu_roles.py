"""
modules/select_menu_roles.py
Select menu role assignment – config I/O, views, slash commands for message creation, and select menus.
"""

from __future__ import annotations

import json
import os

import discord
from discord import app_commands

from modules.utils import load_json, save_json, SELECT_MENU_ROLES_FILE

# ── I/O ───────────────────────────────────────────────────────────────────────

def load_select_menu_roles() -> dict:
    return load_json(SELECT_MENU_ROLES_FILE, {})


def save_select_menu_roles(data: dict):
    save_json(SELECT_MENU_ROLES_FILE, data)


# ── Views ─────────────────────────────────────────────────────────────────────

class RoleSelectMenu(discord.ui.Select):
    def __init__(self, custom_id: str, placeholder: str, options: list, min_values: int, max_values: int):
        discord_options = []
        seen_values = set()
        for option in options[:25]:
            val = str(option.get("value", ""))
            if val in seen_values:
                continue
            seen_values.add(val)
            
            emoji_raw = option.get("emoji")
            emoji = None
            if emoji_raw:
                if isinstance(emoji_raw, dict):
                    emoji_name = emoji_raw.get("name")
                    emoji_id = emoji_raw.get("id")
                    if emoji_id:
                        emoji = discord.PartialEmoji(name=emoji_name, id=int(emoji_id), animated=emoji_raw.get("animated", False))
                    else:
                        emoji = emoji_name
                else:
                    emoji = emoji_raw

            discord_options.append(discord.SelectOption(
                label=option.get("label", "Unknown"),
                value=str(option.get("value", "")),
                description=option.get("description"),
                emoji=emoji,
                default=option.get("default", False)
            ))
        
        num_options = len(discord_options)
        if num_options == 0:
            discord_options.append(discord.SelectOption(label="No options", value="none", description="No role options configured"))
            num_options = 1
        
        min_values = max(1, min(min_values, num_options))
        max_values = max(min_values, min(max_values, num_options))
        
        super().__init__(
            custom_id=custom_id, placeholder=placeholder, options=discord_options,
            min_values=min_values, max_values=max_values
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            guild = interaction.guild
            if not guild:
                await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)
                return

            available_role_ids = set()
            for opt in self.options:
                if opt.value.isdigit():
                    available_role_ids.add(int(opt.value))
                else:
                    role = discord.utils.get(guild.roles, name=opt.value)
                    if role:
                        available_role_ids.add(role.id)

            selected_role_ids = set()
            for val in self.values:
                if val.isdigit():
                    selected_role_ids.add(int(val))
                else:
                    role = discord.utils.get(guild.roles, name=val)
                    if role:
                        selected_role_ids.add(role.id)

            member = interaction.user
            if not isinstance(member, discord.Member):
                member = await guild.fetch_member(member.id)

            bot_member = guild.me
            bot_top_role = bot_member.top_role

            added = []
            removed = []
            errors = []

            for role_id in available_role_ids:
                role = guild.get_role(role_id)
                if not role:
                    continue
                if role.managed:
                    if role_id in selected_role_ids:
                        errors.append(f"{role.name} (managed/integration role)")
                    continue
                if role.position >= bot_top_role.position:
                    if role_id in selected_role_ids and role not in member.roles:
                        errors.append(f"{role.name} (role is higher than bot)")
                    continue
                try:
                    if role_id in selected_role_ids:
                        if role not in member.roles:
                            await member.add_roles(role, reason="Select Menu Role Assignment")
                            added.append(role.mention)
                    else:
                        if role in member.roles:
                            await member.remove_roles(role, reason="Select Menu Role Assignment")
                            removed.append(role.mention)
                except discord.Forbidden:
                    errors.append(f"{role.name} (no permission)")
                except Exception as e:
                    errors.append(f"{role.name} ({str(e)})")

            response = []
            if added:
                response.append(f"{', '.join(added)} has been added. ✅")
            if removed:
                response.append(f"{', '.join(removed)} has been removed. ⚠️")
            if errors:
                response.append(f"⚠️ Couldn't manage: {', '.join(errors)}")
            if not response:
                response.append("ℹ️ No changes made to your roles.")

            await interaction.response.send_message("\n".join(response), ephemeral=True)
        except Exception as e:
            print(f"[SelectMenuRoles] Error in callback: {e}")
            try:
                await interaction.response.send_message(f"❌ Error updating roles: {str(e)}", ephemeral=True)
            except Exception:
                pass


class SelectMenuRoleView(discord.ui.View):
    def __init__(self, components_data: list, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        for component in components_data:
            if component.get("type") == 1:
                for sub in component.get("components", []):
                    if sub.get("type") == 3:
                        select_menu = RoleSelectMenu(
                            custom_id=sub.get("custom_id", "role_select"),
                            placeholder=sub.get("placeholder", "Select a role..."),
                            options=sub.get("options", []),
                            min_values=sub.get("min_values", 1),
                            max_values=sub.get("max_values", 1)
                        )
                        self.add_item(select_menu)


# ── Slash commands ────────────────────────────────────────────────────────────

select_menu_roles_group = app_commands.Group(name="select_roles", description="Manage select menu role messages")

@select_menu_roles_group.command(name="create", description="Create a new select menu role message")
@app_commands.describe(channel="Channel to send the message to", message_data="JSON data for the Discord message")
async def select_menu_roles_create(interaction: discord.Interaction, channel: discord.TextChannel, message_data: str):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ You need the `Manage Roles` permission.", ephemeral=True)
        return
    try:
        data = json.loads(message_data)
        embed = None
        if "embeds" in data and data["embeds"]:
            em = data["embeds"][0]
            desc = "\n".join(em.get("description", [])) if isinstance(em.get("description"), list) else em.get("description")
            embed = discord.Embed(title=em.get("title"), description=desc, color=em.get("color", 0x5865f2))
            if em.get("fields"):
                for field in em["fields"]:
                    embed.add_field(name=field["name"], value=field["value"], inline=field.get("inline", False))
        
        view = SelectMenuRoleView(data.get("components", []), interaction.guild.id)
        content = data.get("content")
        sent_message = await channel.send(content=content, embed=embed, view=view)
        
        roles = load_select_menu_roles()
        roles[str(sent_message.id)] = {
            "guild_id": interaction.guild.id,
            "channel_id": channel.id,
            "message_data": data,
            "created_by": interaction.user.id
        }
        save_select_menu_roles(roles)
        await interaction.response.send_message(f"✅ Select menu role message created in {channel.mention}!", ephemeral=True)
    except json.JSONDecodeError:
        await interaction.response.send_message("❌ Invalid JSON format.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error creating select menu: {e}", ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: discord.ext.commands.Bot):
    bot.tree.add_command(select_menu_roles_group)


def build_select_menu_embed(message_data: dict) -> discord.Embed | None:
    embeds = message_data.get("embeds")
    if not isinstance(embeds, list) or not embeds:
        return None

    embed_data = embeds[0]
    if not isinstance(embed_data, dict):
        return None

    raw_description = embed_data.get("description")
    if isinstance(raw_description, list):
        description = "\n".join(str(line) for line in raw_description)
    elif raw_description is None:
        description = None
    else:
        description = str(raw_description)

    raw_color = embed_data.get("color", 0x5865F2)
    color_value = 0x5865F2
    try:
        if isinstance(raw_color, str):
            text = raw_color.strip()
            if text.startswith("#"):
                color_value = int(text.lstrip("#"), 16)
            elif text:
                color_value = int(text)
        elif raw_color is not None:
            color_value = int(raw_color)
    except (TypeError, ValueError):
        color_value = 0x5865F2

    embed = discord.Embed(
        title=embed_data.get("title"),
        description=description,
        color=color_value,
    )

    thumbnail = embed_data.get("thumbnail")
    if isinstance(thumbnail, dict):
        thumbnail_url = thumbnail.get("url")
        if thumbnail_url:
            embed.set_thumbnail(url=str(thumbnail_url))

    footer = embed_data.get("footer")
    if isinstance(footer, dict):
        footer_text = footer.get("text")
        if footer_text:
            embed.set_footer(text=str(footer_text))

    fields = embed_data.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, dict):
                continue
            name = field.get("name")
            value = field.get("value")
            if name in (None, "") or value in (None, ""):
                continue
            embed.add_field(
                name=str(name),
                value=str(value),
                inline=bool(field.get("inline", False)),
            )

    
    return embed
