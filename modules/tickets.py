"""
modules/tickets.py
Support ticket system – config, views, slash commands.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands

from modules.utils import load_json, save_json, TICKET_FILE


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_ticket_config() -> dict:
    return load_json(TICKET_FILE, {})


def save_ticket_config(data: dict):
    save_json(TICKET_FILE, data)


# ── Views ─────────────────────────────────────────────────────────────────────

class TicketSetupModal(discord.ui.Modal, title="Ticket Panel Setup"):
    embed_title = discord.ui.TextInput(
        label="Embed Title",
        placeholder="e.g. Buy Premium? Click the Ticket!",
        max_length=256,
    )
    embed_description = discord.ui.TextInput(
        label="Embed Description (markdown ok)",
        style=discord.TextStyle.paragraph,
        placeholder="To create a ticket use the Create ticket button below.",
        max_length=4000,
    )
    embed_footer = discord.ui.TextInput(
        label="Footer (optional)",
        placeholder="e.g. TicketTool.xyz - Ticketing without clutter",
        required=False,
        max_length=256,
    )

    def __init__(
        self,
        panel_channel: discord.TextChannel,
        ticket_category: discord.CategoryChannel,
        support_roles: list,
    ):
        super().__init__()
        self.panel_channel = panel_channel
        self.ticket_category = ticket_category
        self.support_roles = support_roles

    async def on_submit(self, interaction: discord.Interaction):
        import random
        _STYLE_COLORS = [
            (discord.ButtonStyle.primary, 0x5865F2),
            (discord.ButtonStyle.success, 0x57F287),
            (discord.ButtonStyle.danger, 0xED4245),
            (discord.ButtonStyle.secondary, 0x4E5058),
        ]
        chosen_style, chosen_color = random.choice(_STYLE_COLORS)

        embed = discord.Embed(
            title=self.embed_title.value,
            description=self.embed_description.value,
            color=chosen_color,
        )
        if self.embed_footer.value:
            embed.set_footer(text=self.embed_footer.value)

        view = CreateTicketView(style=chosen_style)
        panel_msg = await self.panel_channel.send(embed=embed, view=view)

        cfg = load_ticket_config()
        guild_id = str(interaction.guild.id)
        cfg[guild_id] = {
            "panel_channel_id": self.panel_channel.id,
            "panel_message_id": panel_msg.id,
            "ticket_category_id": self.ticket_category.id,
            "support_role_ids": [r.id for r in self.support_roles],
            "open_tickets": cfg.get(guild_id, {}).get("open_tickets", {}),
        }
        save_ticket_config(cfg)
        await interaction.response.send_message(
            f"✅ Ticket panel sent to {self.panel_channel.mention}!", ephemeral=True
        )


class CreateTicketView(discord.ui.View):
    def __init__(self, style: discord.ButtonStyle = discord.ButtonStyle.success):
        super().__init__(timeout=None)
        # Dynamically set style if needed, but discord.py requires standard button decorator to reconstruct this!
        for child in self.children:
            if getattr(child, "custom_id", None) == "ticket:create":
                child.style = style

    @discord.ui.button(label="Create ticket", style=discord.ButtonStyle.success, emoji="🎫", custom_id="ticket:create")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        user = interaction.user
        cfg = load_ticket_config()
        gcfg = cfg.get(str(guild.id))
        if not gcfg:
            await interaction.followup.send("❌ Ticket system is not configured.", ephemeral=True)
            return

        open_tickets = gcfg.get("open_tickets", {})
        if str(user.id) in open_tickets:
            existing = guild.get_channel(int(open_tickets[str(user.id)]))
            if existing:
                await interaction.followup.send(
                    f"❌ You already have an open ticket: {existing.mention}", ephemeral=True
                )
                return
            else:
                del open_tickets[str(user.id)]

        category = guild.get_channel(int(gcfg["ticket_category_id"]))
        if not category:
            await interaction.followup.send("❌ Ticket category not found.", ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        for role_id in gcfg.get("support_role_ids", []):
            role = guild.get_role(int(role_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        ticket_name = f"ticket-{user.name}".lower().replace(" ", "-")[:50]
        channel = await guild.create_text_channel(
            name=ticket_name, category=category, overwrites=overwrites,
            reason=f"Ticket opened by {user}",
        )

        open_tickets[str(user.id)] = channel.id
        gcfg["open_tickets"] = open_tickets
        cfg[str(guild.id)] = gcfg
        save_ticket_config(cfg)

        role_mentions = " ".join(f"<@&{rid}>" for rid in gcfg.get("support_role_ids", []))
        welcome_embed = discord.Embed(
            title="Ticket Opened",
            description=(
                f"Hey {user.mention}, welcome to your ticket!\n\n"
                "Support staff will be with you shortly.\n"
                "Click **Close Ticket** below when your issue is resolved."
            ),
            color=0x5865F2,
            timestamp=datetime.utcnow(),
        )
        welcome_embed.set_footer(text=f"Opened by {user}", icon_url=user.display_avatar.url)
        await channel.send(content=role_mentions or None, embed=welcome_embed, view=CloseTicketView())
        await interaction.followup.send(
            f"✅ Your ticket has been created: {channel.mention}", ephemeral=True
        )


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        channel = interaction.channel
        cfg = load_ticket_config()
        gcfg = cfg.get(str(guild.id), {})

        support_role_ids = set(int(r) for r in gcfg.get("support_role_ids", []))
        user_role_ids = {r.id for r in interaction.user.roles}
        open_tickets = gcfg.get("open_tickets", {})
        is_owner = int(open_tickets.get(str(interaction.user.id), 0)) == channel.id
        is_support = bool(support_role_ids & user_role_ids)
        is_admin = interaction.user.guild_permissions.manage_channels

        if not (is_owner or is_support or is_admin):
            await interaction.response.send_message("❌ You don't have permission to close this ticket.", ephemeral=True)
            return

        await interaction.response.send_message("🔒 Closing ticket in 5 seconds...")
        await asyncio.sleep(5)

        owner_id = next((k for k, v in open_tickets.items() if int(v) == channel.id), None)
        if owner_id:
            del open_tickets[owner_id]
            gcfg["open_tickets"] = open_tickets
            cfg[str(guild.id)] = gcfg
            save_ticket_config(cfg)

        await channel.delete(reason=f"Ticket closed by {interaction.user}")


# ── Slash commands ────────────────────────────────────────────────────────────

ticket_group = app_commands.Group(name="ticket", description="Manage the ticket system")


@ticket_group.command(name="setup", description="Create a ticket panel in a channel")
@app_commands.describe(
    panel_channel="Channel where the ticket embed will be posted",
    ticket_category="Category where new ticket channels will be created",
    support_role1="Role that can see and respond to all tickets",
    support_role2="2nd support role (optional)",
    support_role3="3rd support role (optional)",
    support_role4="4th support role (optional)",
    support_role5="5th support role (optional)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_setup(
    interaction: discord.Interaction,
    panel_channel: discord.TextChannel,
    ticket_category: discord.CategoryChannel,
    support_role1: discord.Role = None,
    support_role2: discord.Role = None,
    support_role3: discord.Role = None,
    support_role4: discord.Role = None,
    support_role5: discord.Role = None,
):
    support_roles = [r for r in [support_role1, support_role2, support_role3, support_role4, support_role5] if r]
    await interaction.response.send_modal(TicketSetupModal(panel_channel, ticket_category, support_roles))


@ticket_group.command(name="close", description="Close the current ticket channel")
@app_commands.checks.has_permissions(manage_channels=True)
async def ticket_close(interaction: discord.Interaction):
    await interaction.response.send_message("🔒 Closing ticket in 5 seconds...")
    cfg = load_ticket_config()
    gcfg = cfg.get(str(interaction.guild.id), {})
    open_tickets = gcfg.get("open_tickets", {})
    owner_id = next((k for k, v in open_tickets.items() if v == interaction.channel.id), None)
    if owner_id:
        del open_tickets[owner_id]
        gcfg["open_tickets"] = open_tickets
        cfg[str(interaction.guild.id)] = gcfg
        save_ticket_config(cfg)
    await asyncio.sleep(5)
    await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: discord.ext.commands.Bot):
    bot.tree.add_command(ticket_group)
    bot.add_view(CreateTicketView())
    bot.add_view(CloseTicketView())
