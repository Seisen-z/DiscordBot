"""
modules/applications.py
Staff application panels — two-part sequential modals matching the Seisen Hub form.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from modules.utils import load_json, save_json

APPS_FILE = "applications"
PANEL_FILE = "app_panels"
COOLDOWN_DAYS = 7

# question tuples: (label, style, required)
_POSITIONS: dict = {
    "staff": {
        "label": "Apply as Staff",
        "emoji": "🛡️",
        "style": discord.ButtonStyle.danger,
        "title": "Staff Application",
        "questions": [
            ("Timezone & hours/week? (1-5/6-10/11-20/20+)", discord.TextStyle.short, True),
            ("Moderation experience? (servers & roles)", discord.TextStyle.paragraph, True),
            ("Handle a toxic/disrespectful member?", discord.TextStyle.paragraph, True),
            ("Why should we pick you as Staff?", discord.TextStyle.paragraph, True),
            ("Why do you want to join the Seisen team?", discord.TextStyle.paragraph, True),
        ],
    },
    "tester": {
        "label": "Apply as Tester",
        "emoji": "🧪",
        "style": discord.ButtonStyle.success,
        "title": "Tester Application",
        "questions": [
            ("Timezone & hours/week? (1-5/6-10/11-20/20+)", discord.TextStyle.short, True),
            ("Prior testing experience? (scripts/exploits)", discord.TextStyle.paragraph, True),
            ("Testing devices you have? (PC/Android/Cloud)", discord.TextStyle.short, True),
            ("How would you report a bug or failure?", discord.TextStyle.paragraph, True),
            ("Why do you want to join the Seisen team?", discord.TextStyle.paragraph, True),
        ],
    },
    "helper": {
        "label": "Apply as Helper",
        "emoji": "🤝",
        "style": discord.ButtonStyle.primary,
        "title": "Helper Application",
        "questions": [
            ("Timezone & hours/week? (1-5/6-10/11-20/20+)", discord.TextStyle.short, True),
            ("How familiar are you with Seisen Hub?", discord.TextStyle.paragraph, True),
            ("How would you help a completely new user?", discord.TextStyle.paragraph, True),
            ("What if you don't know the answer?", discord.TextStyle.paragraph, True),
            ("Why do you want to join the Seisen team?", discord.TextStyle.paragraph, True),
        ],
    },
}


def _check_cooldown(guild_id: int, user_id: int, position: str) -> Optional[str]:
    """Returns an error string if on cooldown, or None if allowed."""
    data = load_json(APPS_FILE, {})
    guild_apps = data.get(str(guild_id), [])
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=COOLDOWN_DAYS)
    for app in guild_apps:
        if app.get("user_id") != str(user_id) or app.get("position") != position:
            continue
        if app.get("status") == "pending":
            pos_label = _POSITIONS[position]["label"].replace("Apply as ", "")
            return f"❌ You already have a pending **{pos_label}** application. Please wait for it to be reviewed."
        submitted_at_str = app.get("submitted_at", "")
        if submitted_at_str:
            try:
                submitted_at = datetime.fromisoformat(submitted_at_str)
                if submitted_at.tzinfo is None:
                    submitted_at = submitted_at.replace(tzinfo=timezone.utc)
                if submitted_at > cutoff:
                    next_at = submitted_at + timedelta(days=COOLDOWN_DAYS)
                    ts = int(next_at.timestamp())
                    pos_label = _POSITIONS[position]["label"].replace("Apply as ", "")
                    return f"❌ You can re-apply for **{pos_label}** <t:{ts}:R>."
            except Exception:
                pass
    return None


# ── Modal ─────────────────────────────────────────────────────────────────────

class ApplicationModal(discord.ui.Modal):
    def __init__(self, position: str, guild_id: int, log_channel_id: Optional[int]):
        cfg = _POSITIONS[position]
        super().__init__(title=cfg["title"])
        self.position = position
        self.guild_id = guild_id
        self.log_channel_id = log_channel_id
        self._question_keys: list[str] = []
        self._inputs: list[discord.ui.TextInput] = []

        for label, style, required in cfg["questions"]:
            inp = discord.ui.TextInput(
                label=label,
                style=style,
                required=required,
                max_length=1000 if style == discord.TextStyle.paragraph else 200,
            )
            self.add_item(inp)
            self._question_keys.append(label)
            self._inputs.append(inp)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        err = _check_cooldown(self.guild_id, interaction.user.id, self.position)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        answers = {q: inp.value for q, inp in zip(self._question_keys, self._inputs) if inp.value}
        app_id = str(uuid.uuid4())[:8].upper()
        now = datetime.now(timezone.utc)

        application = {
            "id": app_id,
            "guild_id": str(self.guild_id),
            "user_id": str(interaction.user.id),
            "username": str(interaction.user),
            "display_name": interaction.user.display_name,
            "avatar_url": str(interaction.user.display_avatar.url),
            "position": self.position,
            "answers": answers,
            "submitted_at": now.isoformat(),
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "notes": None,
        }

        data = load_json(APPS_FILE, {})
        guild_apps: list = data.get(str(self.guild_id), [])
        guild_apps.append(application)
        data[str(self.guild_id)] = guild_apps
        save_json(APPS_FILE, data)

        if self.log_channel_id and interaction.guild:
            ch = interaction.guild.get_channel(int(self.log_channel_id))
            if ch:
                color_map = {
                    "staff": discord.Color.red(),
                    "tester": discord.Color.green(),
                    "helper": discord.Color.blue(),
                }
                pos_label = _POSITIONS[self.position]["label"].replace("Apply as ", "")
                embed = discord.Embed(
                    title=f"📋 New {pos_label} Application — #{app_id}",
                    color=color_map.get(self.position, discord.Color.orange()),
                    timestamp=now,
                )
                embed.set_author(
                    name=f"{interaction.user.display_name} ({interaction.user})",
                    icon_url=interaction.user.display_avatar.url,
                )
                for q, a in answers.items():
                    embed.add_field(name=q, value=(a[:1024] if a else "—"), inline=False)
                embed.set_footer(text=f"ID: {app_id} • Review on the dashboard")
                try:
                    log_msg = await ch.send(embed=embed, view=LogInterviewView())
                    await log_msg.add_reaction("✅")
                    await log_msg.add_reaction("❌")
                except discord.HTTPException:
                    pass

        pos_label = _POSITIONS[self.position]["label"].replace("Apply as ", "")
        await interaction.followup.send(
            f"✅ Your **{pos_label}** application (`#{app_id}`) has been submitted! We'll review it soon.",
            ephemeral=True,
        )


# ── Log Interview View ────────────────────────────────────────────────────────

class LogInterviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Interview Channel", emoji="📝", style=discord.ButtonStyle.primary, custom_id="app:interview")
    async def interview_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You need Manage Server permission to create interview channels.", ephemeral=True)
            return

        # Parse app_id from embed footer: "ID: {app_id} • Review on the dashboard"
        app_id = None
        if interaction.message and interaction.message.embeds:
            footer_text = (interaction.message.embeds[0].footer.text or "")
            if footer_text.startswith("ID: "):
                app_id = footer_text.split("•")[0].replace("ID: ", "").strip()

        if not app_id:
            await interaction.response.send_message("❌ Could not read application ID from this embed.", ephemeral=True)
            return

        data = load_json(APPS_FILE, {})
        guild_apps: list = data.get(str(interaction.guild_id), [])
        app = next((a for a in guild_apps if a.get("id", "").upper() == app_id.upper()), None)

        if not app:
            await interaction.response.send_message(f"❌ Application `#{app_id}` not found.", ephemeral=True)
            return

        user_id = app.get("user_id")
        member = interaction.guild.get_member(int(user_id))
        if not member:
            try:
                member = await interaction.guild.fetch_member(int(user_id))
            except Exception:
                await interaction.response.send_message("❌ Applicant is no longer in this server.", ephemeral=True)
                return

        panels = load_json(PANEL_FILE, {})
        cfg = panels.get(str(interaction.guild_id), {})
        interview_category_id = cfg.get("interview_category_id")
        interviewer_role_ids: list = cfg.get("interviewer_role_ids") or []

        category = None
        if interview_category_id:
            cat = interaction.guild.get_channel(int(interview_category_id))
            if isinstance(cat, discord.CategoryChannel):
                category = cat

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            member: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_messages=True),
        }
        for rid in interviewer_role_ids:
            role = interaction.guild.get_role(int(rid))
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True)

        username = (app.get("username") or "user").split("#")[0].lower()
        safe_name = f"interview-{username}"[:100]

        await interaction.response.defer(ephemeral=True)
        try:
            ch = await interaction.guild.create_text_channel(name=safe_name, category=category, overwrites=overwrites)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to create channel: {e}", ephemeral=True)
            return

        pos_label = _POSITIONS.get(app.get("position", ""), {}).get("label", "").replace("Apply as ", "") or app.get("position", "").title()
        try:
            await ch.send(
                f"Hey {member.mention}! 👋\n\n"
                f"Thank you for applying for **{pos_label}** at **Seisen Hub**. "
                f"Our team has reviewed your application and would like to follow up with you here.\n\n"
                f"Please stay tuned for further instructions!"
            )
        except Exception:
            pass

        button.disabled = True
        button.label = "Interview Channel Created"
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        await interaction.followup.send(f"✅ Interview channel {ch.mention} created for {member.mention}!", ephemeral=True)


# ── Persistent Panel View ─────────────────────────────────────────────────────

class ApplicationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _open_modal(self, interaction: discord.Interaction, position: str):
        err = _check_cooldown(interaction.guild_id, interaction.user.id, position)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        panels = load_json(PANEL_FILE, {})
        cfg = panels.get(str(interaction.guild_id), {})
        raw_lcid = cfg.get("log_channel_id")
        try:
            log_channel_id = int(raw_lcid) if raw_lcid else None
        except (ValueError, TypeError):
            log_channel_id = None
        await interaction.response.send_modal(
            ApplicationModal(position, interaction.guild_id, log_channel_id)
        )

    @discord.ui.button(label="Apply as Staff", emoji="🛡️", style=discord.ButtonStyle.danger, custom_id="app:staff")
    async def staff_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_modal(interaction, "staff")

    @discord.ui.button(label="Apply as Tester", emoji="🧪", style=discord.ButtonStyle.success, custom_id="app:tester")
    async def tester_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_modal(interaction, "tester")

    @discord.ui.button(label="Apply as Helper", emoji="🤝", style=discord.ButtonStyle.primary, custom_id="app:helper")
    async def helper_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_modal(interaction, "helper")


# ── Slash Commands ────────────────────────────────────────────────────────────

apppanel_group = app_commands.Group(name="apppanel", description="Manage staff application panels")


@apppanel_group.command(name="setup", description="Create a staff application panel with buttons")
@app_commands.describe(
    channel="Channel to post the panel in",
    log_channel="Channel where submitted applications are sent",
    title="Panel embed title",
    description="Panel embed description",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def apppanel_setup(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    log_channel: discord.TextChannel,
    title: str = "📋 Seisen Hub Staff Applications",
    description: str = "We are looking for dedicated members to join our team.\n\nClick a button below to apply for a staff position.",
):
    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Applications are reviewed by the staff team.")

    view = ApplicationPanelView()
    try:
        msg = await channel.send(embed=embed, view=view)
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Failed to send panel: {e}", ephemeral=True)
        return

    panels = load_json(PANEL_FILE, {})
    panels[str(interaction.guild_id)] = {
        "channel_id": channel.id,
        "message_id": msg.id,
        "log_channel_id": log_channel.id,
        "title": title,
        "description": description,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": interaction.user.id,
    }
    save_json(PANEL_FILE, panels)

    await interaction.followup.send(
        f"✅ Application panel created in {channel.mention}! Submissions will be logged to {log_channel.mention}.",
        ephemeral=True,
    )


@apppanel_group.command(name="delete", description="Delete the application panel for this server")
@app_commands.checks.has_permissions(manage_guild=True)
async def apppanel_delete(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    panels = load_json(PANEL_FILE, {})
    cfg = panels.pop(str(interaction.guild_id), None)
    if not cfg:
        await interaction.followup.send("❌ No application panel found for this server.", ephemeral=True)
        return
    ch = interaction.guild.get_channel(cfg.get("channel_id"))
    if ch:
        try:
            msg = await ch.fetch_message(cfg["message_id"])
            await msg.delete()
        except Exception:
            pass
    save_json(PANEL_FILE, panels)
    await interaction.followup.send("🗑️ Application panel deleted.", ephemeral=True)


# ── /application lookup ───────────────────────────────────────────────────────

@app_commands.command(name="application", description="Look up a submitted application by its ID code")
@app_commands.describe(code="The application ID shown after you submitted (e.g. 7C280196)")
async def application_lookup(interaction: discord.Interaction, code: str):
    await interaction.response.defer(ephemeral=True)

    code = code.strip().upper()
    data = load_json(APPS_FILE, {})
    guild_apps: list = data.get(str(interaction.guild_id), [])

    app = next((a for a in guild_apps if a.get("id", "").upper() == code), None)

    if app is None:
        await interaction.followup.send(
            f"❌ No application with ID `#{code}` was found in this server.",
            ephemeral=True,
        )
        return

    is_own = str(interaction.user.id) == str(app.get("user_id"))
    is_staff = interaction.user.guild_permissions.manage_guild

    if not is_own and not is_staff:
        await interaction.followup.send(
            "❌ You can only look up your own applications.",
            ephemeral=True,
        )
        return

    position = app.get("position", "")
    pos_cfg = _POSITIONS.get(position, {})
    pos_label = pos_cfg.get("label", position.title()).replace("Apply as ", "")

    color_map = {
        "staff":  discord.Color.red(),
        "tester": discord.Color.green(),
        "helper": discord.Color.blue(),
    }
    status = app.get("status", "pending")
    status_emoji = {"pending": "🕐", "accepted": "✅", "rejected": "❌"}.get(status, "🕐")
    status_label = status.capitalize()

    submitted_at_str = app.get("submitted_at", "")
    try:
        dt = datetime.fromisoformat(submitted_at_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = f"<t:{int(dt.timestamp())}:F>"
    except Exception:
        ts = submitted_at_str or "Unknown"

    embed = discord.Embed(
        title=f"📋 {pos_label} Application — #{code}",
        color=color_map.get(position, discord.Color.blurple()),
        timestamp=datetime.now(timezone.utc),
    )

    applicant_name = app.get("display_name") or app.get("username") or "Unknown"
    avatar = app.get("avatar_url")
    if avatar:
        embed.set_author(name=applicant_name, icon_url=avatar)
    else:
        embed.set_author(name=applicant_name)

    embed.add_field(name="Status", value=f"{status_emoji} {status_label}", inline=True)
    embed.add_field(name="Position", value=pos_label, inline=True)
    embed.add_field(name="Submitted", value=ts, inline=True)

    answers: dict = app.get("answers", {})
    for question, answer in answers.items():
        embed.add_field(
            name=question,
            value=(answer[:1024] if answer else "—"),
            inline=False,
        )

    if status == "accepted":
        embed.set_footer(text=f"ID: #{code} • Your application has been accepted — welcome to the team!")
    elif status == "rejected":
        embed.set_footer(text=f"ID: #{code} • Your application was not accepted this time.")
    else:
        embed.set_footer(text=f"ID: #{code} • Your application is pending review.")

    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: commands.Bot):
    bot.tree.add_command(apppanel_group)
    bot.tree.add_command(application_lookup)
    bot.add_view(ApplicationPanelView())
    bot.add_view(LogInterviewView())
