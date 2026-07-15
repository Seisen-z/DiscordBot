"""
modules/general.py
General utility and announce commands.
"""

from __future__ import annotations
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands
from modules.utils import ANNOUNCE_DRAFTS_FILE, load_json, normalize_command_name, save_json
from modules.access_control import owner_or_allowed_for_locked_commands
from modules.access_control import load_command_access


def load_announce_drafts() -> dict:
    data = load_json(ANNOUNCE_DRAFTS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_announce_drafts(data: dict):
    save_json(ANNOUNCE_DRAFTS_FILE, data)

# ── Announce Modal ────────────────────────────────────────────────────────────

class AnnounceModal(discord.ui.Modal, title="New Announcement"):
    def __init__(
        self,
        channel: discord.TextChannel,
        role: discord.Role = None,
        draft: dict = None,
        save_as_draft: str = None,
    ):
        super().__init__()
        self.channel = channel
        self.role = role
        self.save_as_draft = save_as_draft  # if set, save the submission as a draft with this name

        d = draft or {}
        self.announce_title = discord.ui.TextInput(
            label="Title",
            placeholder="e.g. **New Update!!**",
            max_length=256,
            default=d.get("title", ""),
        )
        self.description = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            placeholder="Announcement body...",
            max_length=4000,
            default=d.get("description", ""),
        )
        self.thumbnail_url = discord.ui.TextInput(
            label="Thumbnail URL (optional)",
            required=False,
            max_length=500,
            default=d.get("thumbnail_url", ""),
        )
        self.footer = discord.ui.TextInput(
            label="Footer (optional)",
            placeholder="e.g. 📢 Game Update • Garden Horizon",
            required=False,
            max_length=256,
            default=d.get("footer", ""),
        )
        self.add_item(self.announce_title)
        self.add_item(self.description)
        self.add_item(self.thumbnail_url)
        self.add_item(self.footer)

    async def on_submit(self, interaction: discord.Interaction):
        # Optionally save/update the draft
        if self.save_as_draft:
            drafts = load_announce_drafts()
            guild_id = str(interaction.guild_id)
            if guild_id not in drafts:
                drafts[guild_id] = {}
            drafts[guild_id][self.save_as_draft] = {
                "title": self.announce_title.value,
                "description": self.description.value,
                "thumbnail_url": self.thumbnail_url.value,
                "footer": self.footer.value,
            }
            save_announce_drafts(drafts)

        embed = discord.Embed(
            title=self.announce_title.value,
            description=self.description.value,
            color=5763719,
            timestamp=datetime.utcnow(),
        )
        if self.thumbnail_url.value:
            embed.set_thumbnail(url=self.thumbnail_url.value)
        if self.footer.value:
            embed.set_footer(text=self.footer.value)

        content = self.role.mention if self.role else None
        await self.channel.send(content=content, embed=embed)

        extra = f" Draft **{self.save_as_draft}** updated." if self.save_as_draft else ""
        await interaction.response.send_message(
            f"✅ Announcement sent to {self.channel.mention}!{extra}", ephemeral=True
        )


class SaveDraftModal(discord.ui.Modal, title="Save Announcement Draft"):
    def __init__(self, draft_name: str, channel: discord.TextChannel, role: discord.Role = None, existing: dict = None):
        super().__init__()
        self.draft_name = draft_name
        self.channel = channel
        self.role = role

        d = existing or {}
        self.announce_title = discord.ui.TextInput(
            label="Title",
            placeholder="e.g. **New Update!!**",
            max_length=256,
            default=d.get("title", ""),
        )
        self.description = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            placeholder="Announcement body...",
            max_length=4000,
            default=d.get("description", ""),
        )
        self.thumbnail_url = discord.ui.TextInput(
            label="Thumbnail URL (optional)",
            required=False,
            max_length=500,
            default=d.get("thumbnail_url", ""),
        )
        self.footer = discord.ui.TextInput(
            label="Footer (optional)",
            placeholder="e.g. 📢 Game Update • Garden Horizon",
            required=False,
            max_length=256,
            default=d.get("footer", ""),
        )
        self.add_item(self.announce_title)
        self.add_item(self.description)
        self.add_item(self.thumbnail_url)
        self.add_item(self.footer)

    async def on_submit(self, interaction: discord.Interaction):
        drafts = load_announce_drafts()
        guild_id = str(interaction.guild_id)
        if guild_id not in drafts:
            drafts[guild_id] = {}
        drafts[guild_id][self.draft_name] = {
            "channel_id": self.channel.id,
            "role_id": self.role.id if self.role else None,
            "title": self.announce_title.value,
            "description": self.description.value,
            "thumbnail_url": self.thumbnail_url.value,
            "footer": self.footer.value,
        }
        save_announce_drafts(drafts)
        role_info = f" | Ping: {self.role.mention}" if self.role else ""
        await interaction.response.send_message(
            f"✅ Draft **{self.draft_name}** saved! Channel: {self.channel.mention}{role_info}\n"
            f"Use `/announce load:{self.draft_name}` to send it anytime.",
            ephemeral=True,
        )


@app_commands.command(name="announce", description="Send an announcement via a popup form")
@app_commands.describe(
    channel="The channel to send the announcement to",
    role="Role to ping with the announcement (optional)",
    load="Load a saved draft by name — pre-fills the form and uses the saved channel/role",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def announce(
    interaction: discord.Interaction,
    channel: discord.TextChannel = None,
    role: discord.Role = None,
    load: str = None,
):
    if load:
        # Load draft — channel and role come from the saved draft
        drafts = load_announce_drafts()
        guild_drafts = drafts.get(str(interaction.guild_id), {})
        if load not in guild_drafts:
            await interaction.response.send_message(
                f"❌ Draft **{load}** not found. Use `/announce_draft list` to see available drafts.",
                ephemeral=True,
            )
            return
        d = guild_drafts[load]
        resolved_channel = interaction.guild.get_channel(d["channel_id"])
        if not resolved_channel:
            await interaction.response.send_message(
                f"❌ The channel saved in draft **{load}** no longer exists. Re-save the draft with a valid channel.",
                ephemeral=True,
            )
            return
        resolved_role = interaction.guild.get_role(d["role_id"]) if d.get("role_id") else None
        try:
            await interaction.response.send_modal(
                AnnounceModal(resolved_channel, resolved_role, draft=d, save_as_draft=load)
            )
        except discord.NotFound:
            pass
    else:
        if not channel:
            await interaction.response.send_message(
                "❌ Please provide a `channel`, or use `load:` to send a saved draft.", ephemeral=True
            )
            return
        try:
            await interaction.response.send_modal(AnnounceModal(channel, role))
        except discord.NotFound:
            pass


announce_draft_group = app_commands.Group(name="announce_draft", description="Manage announcement drafts")

@announce_draft_group.command(name="save", description="Save a new announcement draft (includes channel & role)")
@app_commands.describe(
    name="Name for this draft",
    channel="Channel this draft will send to",
    role="Role to ping when this draft is sent (optional)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def announce_draft_save(interaction: discord.Interaction, name: str, channel: discord.TextChannel, role: discord.Role = None):
    # If draft already exists, pre-fill the modal with existing content
    drafts = load_announce_drafts()
    existing = drafts.get(str(interaction.guild_id), {}).get(name)
    await interaction.response.send_modal(SaveDraftModal(name, channel, role, existing=existing))

@announce_draft_group.command(name="list", description="List all saved announcement drafts")
@app_commands.checks.has_permissions(manage_guild=True)
async def announce_draft_list(interaction: discord.Interaction):
    drafts = load_announce_drafts()
    guild_drafts = drafts.get(str(interaction.guild_id), {})
    if not guild_drafts:
        await interaction.response.send_message("📭 No drafts saved yet. Use `/announce_draft save` to create one.", ephemeral=True)
        return
    lines = []
    for name, d in guild_drafts.items():
        channel = interaction.guild.get_channel(d.get("channel_id", 0))
        role = interaction.guild.get_role(d["role_id"]) if d.get("role_id") else None
        ch_str = channel.mention if channel else "*(deleted channel)*"
        role_str = f" | ping {role.mention}" if role else ""
        preview = (d.get("title") or "*(no title)*")[:50]
        lines.append(f"• **{name}** → {ch_str}{role_str} — {preview}")
    embed = discord.Embed(title="📋 Announcement Drafts", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@announce_draft_group.command(name="delete", description="Delete a saved announcement draft")
@app_commands.describe(name="Name of the draft to delete")
@app_commands.checks.has_permissions(manage_guild=True)
async def announce_draft_delete(interaction: discord.Interaction, name: str):
    drafts = load_announce_drafts()
    guild_id = str(interaction.guild_id)
    if name not in drafts.get(guild_id, {}):
        await interaction.response.send_message(f"❌ Draft **{name}** not found.", ephemeral=True)
        return
    del drafts[guild_id][name]
    save_announce_drafts(drafts)
    await interaction.response.send_message(f"🗑️ Draft **{name}** deleted.", ephemeral=True)


@app_commands.command(name="command", description="List commands you have access to in this server")
@app_commands.describe(filter="Optional substring to filter command names")
async def command_list(interaction: discord.Interaction, filter: str = None):
    if interaction.guild is None:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild_data = load_command_access().get(str(interaction.guild.id), {})
    access = guild_data.get("commands", guild_data) if isinstance(guild_data, dict) else {}
    needle = str(filter or "").lower().strip()

    is_owner = interaction.guild.owner_id == interaction.user.id
    user_role_ids = {str(r.id) for r in getattr(interaction.user, "roles", [])}

    embed = discord.Embed(
        title="🛡️ My Command Access",
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )

    found_any = False
    count = 0
    for cmd_name, role_ids in sorted(access.items()):
        if count >= 24:
            break
        if needle and needle not in cmd_name.lower():
            continue
        if not is_owner and not (user_role_ids & {str(r) for r in role_ids}):
            continue
        found_any = True
        count += 1
        embed.add_field(name=f"/{cmd_name}", value="​", inline=True)

    if not found_any:
        await interaction.followup.send("You don't have access to any role-restricted commands in this server.", ephemeral=True)
        return

    embed.set_footer(text=f"{count} command(s) available to you", icon_url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed, ephemeral=True)


@app_commands.command(name="help", description="Show all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Bot Commands",
        description="All commands use `/` prefix",
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="General", value=(
        "`/help`       — Show this message\n"
        "`/ping`       — Check bot latency\n"
        "`/hello`      — Get a greeting\n"
        "`/say <msg>`  — Make the bot say something\n"
        "`/roll <NdN>` — Roll dice (e.g. `/roll 2d6`)\n"
        "`/command [filter]` — List commands with role access configured\n"
        "`/announce channel: [role:]` — Send an announcement via popup form\n"
        "`/announce load:<name>` — Load & send a saved draft (editable before sending)\n"
        "`/announce_draft save <name> <#channel> [role]` — Save a draft with channel & role\n"
        "`/announce_draft list` — List saved drafts\n"
        "`/announce_draft delete <name>` — Delete a draft"
    ), inline=False)
    embed.add_field(name="Info", value=(        "`/check [query]`     â€” Check user profile by ID, name, or mention\n"        "`/userinfo [@user]`  — Show user info\n"
        "`/serverinfo`        — Show server info\n"
        "`/avatar [@user]`    — Show user avatar"
    ), inline=False)
    embed.add_field(name="Moderation", value=(
        "`/clear <amount>`  — Delete messages (1-100)\n"
        "`/kick <@user> [reason]`  — Kick a member\n"
        "`/ban  <@user> [reason]`  — Ban a member\n"
        "`/unban <user_id> [reason]`  — Unban a user by ID\n"
        "`/banlist`  — List all banned users\n"
        "`/baninfo <user_id>`  — View a banned user's profile\n"
        "`/invite`  — Get a permanent invite link for this server\n"
        "`/invitelist`  — List all active invites in the server"
    ), inline=False)
    embed.add_field(name="Owner/Developer Controls", value=(
        "`/access role <command> <@role>`  — Allow a role to use a locked command\n"
        "`/access remove <command> <@role>`  — Remove a role's access\n"
        "`/access lock <command>`  — Re-lock a command to owner-only\n"
        "`/access list [command]`  — View current access map\n"
        "`/bot_profile name <nickname>` — Change bot's nickname in this server\n"
        "`/bot_profile avatar <image>` — Change bot's avatar in this server\n"
        "`/bot_profile description <text>` — Change bot's About Me in this server\n"
        "`/bot_profile reset` — Reset local profile overrides to defaults\n"
        "`/sync` — Sync slash commands globally (Bot Owner only)"
    ), inline=False)
    embed.add_field(name="Auto Reply", value=(
        "`/autoreply add`          — Add a keyword auto reply (opens form)\n"
        "`/autoreply edit`         — Edit embed content of a rule (opens pre-filled form)\n"
        "`/autoreply edittargets`  — Change which channels/categories a rule watches\n"
        "`/autoreply remove`       — Remove a rule\n"
        "`/autoreply list`         — List all active rules"
    ), inline=False)
    embed.add_field(name="Tickets", value=(
        "`/ticket setup`  — Create a ticket panel in a channel\n"
        "`/ticket close`  — Close the current ticket channel"
    ), inline=False)
    embed.add_field(name="Roblox Monitor", value=(
        "`/roblox setup <universe_id> <#channel>`  — Add a game to monitor for updates\n"
        "`/roblox status`                          — Show all monitor configs\n"
        "`/roblox remove [universe_id]`            — Stop monitoring (all or specific game)\n"
        "`/roblox list`                            — List all monitored games\n"
        "`/roblox test <universe_id>`               — Send test notification"
    ), inline=False)
    embed.add_field(name="Sticky Messages", value=(
        "`/sticky set <#channel> <message>`  — Set a sticky message that reposts after new messages\n"
        "`/sticky remove <#channel>`         — Remove the sticky message"
    ), inline=False)
    embed.add_field(name="Poll", value=(
        "`/poll create <question> <choice1> <choice2> [...] [duration_hours] [multiple]`  — Start a native Discord poll with live voter-only results"
    ), inline=False)
    embed.add_field(name="Giveaways", value=(
        "`/giveaway create <#channel> <reward> [description] [winners] [duration_minutes] [ping_role] [key_log_channel] [key_tier]`  — Start a reaction-based giveaway\n"
        "`/giveaway end <message_id>`  — End a giveaway immediately and announce winners\n"
        "`/giveaway reroll <message_id> [winners]`  — Pick new winner(s) from entrants\n"
        "`/giveaway delete <message_id>`  — Delete an ended giveaway\n"
        "`/giveaway list`  — List recent giveaway IDs and status"
    ), inline=False)
    embed.add_field(name="Member Counter", value=(
        "`/membercounter create <voice|text> [prefix] [include_bots] [category]`  — Create a realtime member counter channel\n"
        "`/membercounter set <channel_id> [prefix] [include_bots] [enabled]`  — Use an existing channel as counter\n"
        "`/membercounter sync`  — Force-update the counter channel now\n"
        "`/membercounter disable`  — Stop automatic counter updates\n"
        "`/membercounter status`  — View current counter configuration"
    ), inline=False)
    embed.add_field(name="Vouch", value=(
        "`/vouch <message> <stars> [proof]` — Leave a vouch for the server\n"
        "`/vouch_setup <#channel>`          — Set the channel for vouches (Admin only)"
    ), inline=False)
    embed.add_field(name="Music", value=(
        "`/music play <query/url>`  — Play and auto-join your voice channel\n"
        "`/music now`  — Show current track\n"
        "`/music pause`  — Pause current track\n"
        "`/music resume`  — Resume playback\n"
        "`/music queue`  — Show current queue\n"
        "`/music loop <off|one|all>`  — Set loop mode\n"
        "`/music shuffle`  — Shuffle queued tracks\n"
        "`/music skip`  — Skip current track\n"
        "`/music stop`  — Stop and clear queue\n"
        "`/music leave`  — Leave voice channel\n"
        "Auto-disconnect after **{MUSIC_IDLE_TIMEOUT}s** idle"
    ), inline=False)
    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@commands.command()
@commands.is_owner()
async def sync(ctx: commands.Context):
    """Sync the slash command tree globally"""
    await ctx.bot.tree.sync()
    await ctx.send("✅ Slash commands synced globally!")

@app_commands.command(name="sync", description="Sync slash commands with Discord (Owner only)")
@app_commands.default_permissions(administrator=True)
async def sync_slash(interaction: discord.Interaction):
    """Slash command version of sync for bot owners"""
    if interaction.user.id != interaction.client.owner_id:
        await interaction.response.send_message("❌ This command is only for the bot owner.", ephemeral=True)
        return
    
    await interaction.response.defer()
    try:
        synced = await interaction.client.tree.sync()
        await interaction.followup.send(f"✅ Slash commands synced successfully! ({len(synced)} commands)")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to sync commands: {str(e)}")


# ── Bot Profile Management (Guild-Specific) ───────────────────────────────────

bot_profile_group = app_commands.Group(name="bot_profile", description="Manage the bot's server-specific profile settings")

@bot_profile_group.command(name="name", description="Change the bot's nickname in this server")
@app_commands.describe(nickname="The new nickname for the bot in this server")
async def bot_profile_name(interaction: discord.Interaction, nickname: str):
    if interaction.guild is None:
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return

    is_bot_owner = await interaction.client.is_owner(interaction.user)
    is_guild_owner = interaction.user.id == interaction.guild.owner_id
    has_perm = interaction.user.guild_permissions.manage_guild

    if not (is_bot_owner or is_guild_owner or has_perm):
        await interaction.response.send_message("❌ You do not have permission to run this command. You need to be the Bot Owner, Server Owner, or have 'Manage Server' permissions.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        from discord.http import Route
        route = Route('PATCH', '/guilds/{guild_id}/members/@me', guild_id=interaction.guild_id)
        await interaction.client.http.request(route, json={'nick': nickname})
        await interaction.followup.send(f"✅ Bot nickname in this server updated to **{nickname}** successfully!", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Failed to update nickname: {e.text or str(e)}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: {str(e)}", ephemeral=True)


@bot_profile_group.command(name="avatar", description="Change the bot's avatar in this server")
@app_commands.describe(attachment="The new avatar image file")
async def bot_profile_avatar(interaction: discord.Interaction, attachment: discord.Attachment):
    if interaction.guild is None:
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return

    is_bot_owner = await interaction.client.is_owner(interaction.user)
    is_guild_owner = interaction.user.id == interaction.guild.owner_id
    has_perm = interaction.user.guild_permissions.manage_guild

    if not (is_bot_owner or is_guild_owner or has_perm):
        await interaction.response.send_message("❌ You do not have permission to run this command. You need to be the Bot Owner, Server Owner, or have 'Manage Server' permissions.", ephemeral=True)
        return

    if not attachment.content_type or not attachment.content_type.startswith("image/"):
        await interaction.response.send_message("❌ The uploaded file must be a valid image.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        import base64
        from discord.http import Route
        
        image_bytes = await attachment.read()
        content_type = attachment.content_type or 'image/png'
        encoded = base64.b64encode(image_bytes).decode('utf-8')
        avatar_data = f"data:{content_type};base64,{encoded}"

        route = Route('PATCH', '/guilds/{guild_id}/members/@me', guild_id=interaction.guild_id)
        await interaction.client.http.request(route, json={'avatar': avatar_data})
        await interaction.followup.send("✅ Bot profile avatar in this server updated successfully!", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Failed to update avatar: {e.text or str(e)}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: {str(e)}", ephemeral=True)


@bot_profile_group.command(name="description", description="Change the bot's About Me description in this server")
@app_commands.describe(text="The new About Me description for the bot in this server")
async def bot_profile_description(interaction: discord.Interaction, text: str):
    if interaction.guild is None:
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return

    is_bot_owner = await interaction.client.is_owner(interaction.user)
    is_guild_owner = interaction.user.id == interaction.guild.owner_id
    has_perm = interaction.user.guild_permissions.manage_guild

    if not (is_bot_owner or is_guild_owner or has_perm):
        await interaction.response.send_message("❌ You do not have permission to run this command. You need to be the Bot Owner, Server Owner, or have 'Manage Server' permissions.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        from discord.http import Route
        route = Route('PATCH', '/guilds/{guild_id}/members/@me', guild_id=interaction.guild_id)
        await interaction.client.http.request(route, json={'bio': text})
        await interaction.followup.send("✅ Bot About Me description in this server updated successfully!", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Failed to update description: {e.text or str(e)}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: {str(e)}", ephemeral=True)


@bot_profile_group.command(name="reset", description="Reset all server-specific bot profile settings to defaults")
async def bot_profile_reset(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return

    is_bot_owner = await interaction.client.is_owner(interaction.user)
    is_guild_owner = interaction.user.id == interaction.guild.owner_id
    has_perm = interaction.user.guild_permissions.manage_guild

    if not (is_bot_owner or is_guild_owner or has_perm):
        await interaction.response.send_message("❌ You do not have permission to run this command. You need to be the Bot Owner, Server Owner, or have 'Manage Server' permissions.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        from discord.http import Route
        route = Route('PATCH', '/guilds/{guild_id}/members/@me', guild_id=interaction.guild_id)
        await interaction.client.http.request(route, json={'nick': None, 'avatar': None, 'bio': None})
        await interaction.followup.send("✅ Bot profile in this server has been reset to global defaults!", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Failed to reset profile: {e.text or str(e)}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: {str(e)}", ephemeral=True)


def register(bot: commands.Bot):
    bot.add_command(sync)
    bot.tree.add_command(sync_slash)
    bot.tree.add_command(announce)
    bot.tree.add_command(help_command)
    bot.tree.add_command(command_list)
    bot.tree.add_command(announce_draft_group)
    bot.tree.add_command(bot_profile_group)
