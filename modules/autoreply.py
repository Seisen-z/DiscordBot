"""
modules/autoreply.py
Auto reply system – modals, slash commands, and on_message handler logic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import discord
from discord import app_commands

from modules.utils import load_json, save_json, AUTOREPLY_FILE


# ── Helpers ───────────────────────────────────────────────────────────────────

def _t_id(t) -> int:
    return int(t["id"]) if isinstance(t, dict) else int(str(t).replace("category-", ""))

def _t_name(t) -> str:
    if isinstance(t, dict): return t.get("name", str(t.get("id")))
    return str(t).replace("category-", "Category: ")

def _t_is_cat(t) -> bool:
    return t.get("is_category", False) if isinstance(t, dict) else str(t).startswith("category-")


def load_autoreplies() -> list:
    return load_json(AUTOREPLY_FILE, [])


def save_autoreplies(data: list):
    save_json(AUTOREPLY_FILE, data)


def parse_hex_color(value: str) -> int:
    v = str(value or "").strip().lstrip("#")
    if len(v) == 6:
        try:
            return int(v, 16)
        except ValueError:
            pass
    return 5763719


BUTTON_COLORS = [
    discord.ButtonStyle.primary,
    discord.ButtonStyle.success,
    discord.ButtonStyle.danger,
    discord.ButtonStyle.secondary,
    discord.ButtonStyle.primary,
]


# ── Modals ─────────────────────────────────────────────────────────────────────

class AutoReplyModal(discord.ui.Modal, title="Add Auto Reply"):
    keyword = discord.ui.TextInput(label="Trigger Keywords (comma-separated)", placeholder="e.g. prem, premium", max_length=300)
    embed_title = discord.ui.TextInput(label="Embed Title", placeholder="e.g. 🌟 Premium Membership", max_length=256)
    embed_description = discord.ui.TextInput(label="Embed Description (markdown ok)", style=discord.TextStyle.paragraph, max_length=4000)
    thumbnail_and_footer = discord.ui.TextInput(label="Thumbnail | Footer | #Color  (split with |)", placeholder="https://img.png | 📢 Footer | #2b2d31", required=False, max_length=700)
    buttons = discord.ui.TextInput(label="Buttons — Label:URL (up to 5, one per line)", style=discord.TextStyle.paragraph, placeholder="Buy Now:https://example.com", required=False, max_length=600)

    def __init__(self, targets: list, delete_after: int = 0):
        super().__init__()
        self.targets = targets
        self.delete_after = delete_after

    async def on_submit(self, interaction: discord.Interaction):
        autoreplies = load_autoreplies()
        new_keywords = [k.strip().lower() for k in self.keyword.value.split(",") if k.strip()]
        target_ids = {t["id"] for t in self.targets}
        tf_parts = self.thumbnail_and_footer.value.split("|")
        embed_thumbnail = tf_parts[0].strip() or None
        embed_footer = tf_parts[1].strip() if len(tf_parts) > 1 else None
        embed_color = parse_hex_color(tf_parts[2].strip()) if len(tf_parts) > 2 and tf_parts[2].strip() else 5763719
        parsed_buttons = []
        for line in self.buttons.value.strip().splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            idx = line.index(":")
            btn_label, btn_url = line[:idx].strip(), line[idx + 1:].strip()
            if btn_label and btn_url.startswith("http"):
                parsed_buttons.append({"label": btn_label, "url": btn_url})
            if len(parsed_buttons) == 5:
                break
        existing_kws = set()
        for entry in autoreplies:
            if {_t_id(t) for t in entry.get("targets", [])} & target_ids:
                existing_kws.update(entry.get("keywords", []))
        dupes = [k for k in new_keywords if k in existing_kws]
        if dupes:
            await interaction.response.send_message(f"❌ These keywords already exist: `{', '.join(dupes)}`", ephemeral=True)
            return
        autoreplies.append({
            "keywords": new_keywords, "targets": self.targets,
            "embed_title": self.embed_title.value, "embed_description": self.embed_description.value,
            "embed_thumbnail": embed_thumbnail, "embed_footer": embed_footer,
            "embed_color": embed_color, "buttons": parsed_buttons, "delete_after": self.delete_after,
        })
        save_autoreplies(autoreplies)
        kw_display = ", ".join(f"`{k}`" for k in new_keywords)
        target_display = ", ".join(f"**{_t_name(t)}**" for t in self.targets)
        delay_info = f"\nAuto-delete after: **{self.delete_after}s**" if self.delete_after else ""
        await interaction.response.send_message(f"✅ Auto reply added! Keywords: {kw_display}\nTargets: {target_display}{delay_info}", ephemeral=True)


class EditAutoReplyModal(discord.ui.Modal, title="Edit Auto Reply"):
    keyword = discord.ui.TextInput(label="Trigger Keywords (comma-separated)", max_length=300)
    embed_title = discord.ui.TextInput(label="Embed Title", max_length=256)
    embed_description = discord.ui.TextInput(label="Embed Description (markdown ok)", style=discord.TextStyle.paragraph, max_length=4000)
    thumbnail_and_footer = discord.ui.TextInput(label="Thumbnail | Footer | #Color  (split with |)", required=False, max_length=700)
    buttons = discord.ui.TextInput(label="Buttons — Label:URL (up to 5, one per line)", style=discord.TextStyle.paragraph, required=False, max_length=600)

    def __init__(self, entry_index: int, existing: dict, new_targets: list = None, delete_after: int = None):
        super().__init__()
        self.entry_index = entry_index
        self.new_targets = new_targets
        self.delete_after = delete_after
        self.keyword.default = ", ".join(existing.get("keywords", []))
        self.embed_title.default = existing.get("embed_title", "")
        self.embed_description.default = existing.get("embed_description", "")
        t = existing.get("embed_thumbnail") or ""
        f = existing.get("embed_footer") or ""
        c = existing.get("embed_color", 5763719)
        self.thumbnail_and_footer.default = f"{t} | {f} | #{c:06X}"
        btns = existing.get("buttons", [])
        if btns:
            self.buttons.default = "\n".join(f"{b['label']}:{b['url']}" for b in btns)

    async def on_submit(self, interaction: discord.Interaction):
        autoreplies = load_autoreplies()
        if self.entry_index >= len(autoreplies):
            await interaction.response.send_message("❌ Rule no longer exists.", ephemeral=True)
            return
        new_keywords = [k.strip().lower() for k in self.keyword.value.split(",") if k.strip()]
        targets = self.new_targets if self.new_targets is not None else autoreplies[self.entry_index].get("targets", [])
        target_ids = {t["id"] for t in targets}
        tf_parts = self.thumbnail_and_footer.value.split("|")
        embed_thumbnail = tf_parts[0].strip() or None
        embed_footer = tf_parts[1].strip() if len(tf_parts) > 1 else None
        embed_color = parse_hex_color(tf_parts[2].strip()) if len(tf_parts) > 2 and tf_parts[2].strip() else autoreplies[self.entry_index].get("embed_color", 5763719)
        parsed_buttons = []
        for line in self.buttons.value.strip().splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            idx = line.index(":")
            btn_label, btn_url = line[:idx].strip(), line[idx + 1:].strip()
            if btn_label and btn_url.startswith("http"):
                parsed_buttons.append({"label": btn_label, "url": btn_url})
            if len(parsed_buttons) == 5:
                break
        existing_kws = set()
        for i, entry in enumerate(autoreplies):
            if i == self.entry_index:
                continue
            if {_t_id(t) for t in entry.get("targets", [])} & target_ids:
                existing_kws.update(entry.get("keywords", []))
        dupes = [k for k in new_keywords if k in existing_kws]
        if dupes:
            await interaction.response.send_message(f"❌ Duplicate keywords: `{', '.join(dupes)}`", ephemeral=True)
            return
        delete_after = self.delete_after if self.delete_after is not None else autoreplies[self.entry_index].get("delete_after", 0)
        autoreplies[self.entry_index] = {
            "keywords": new_keywords, "targets": targets,
            "embed_title": self.embed_title.value, "embed_description": self.embed_description.value,
            "embed_thumbnail": embed_thumbnail, "embed_footer": embed_footer,
            "embed_color": embed_color, "buttons": parsed_buttons, "delete_after": delete_after,
        }
        save_autoreplies(autoreplies)
        kw_display = ", ".join(f"`{k}`" for k in new_keywords)
        await interaction.response.send_message(f"✅ Auto reply updated! Keywords: {kw_display}", ephemeral=True)


# ── on_message handler ────────────────────────────────────────────────────────

async def handle_autoreply(message: discord.Message):
    """Call from on_message – sends matching auto reply embed."""
    if not message.guild or message.author.bot:
        return
    autoreplies = load_autoreplies()
    if not autoreplies:
        return
    msg_lower = message.content.lower()
    channel_id = message.channel.id
    category_id = getattr(message.channel, "category_id", None)
    for entry in autoreplies:
        if not any(kw.lower() in msg_lower for kw in entry.get("keywords", [])):
            continue
        target_hit = False
        for t in entry.get("targets", []):
            if _t_is_cat(t):
                if category_id and str(_t_id(t)) == str(category_id):
                    target_hit = True
                    break
            else:
                if str(_t_id(t)) == str(channel_id):
                    target_hit = True
                    break
        if not target_hit:
            continue
        embed = discord.Embed(
            title=entry.get("embed_title", ""),
            description=entry.get("embed_description", ""),
            color=entry.get("embed_color", 5763719),
        )
        if entry.get("embed_thumbnail"):
            embed.set_thumbnail(url=entry["embed_thumbnail"])
        if entry.get("embed_footer"):
            embed.set_footer(text=entry["embed_footer"])
        view = discord.ui.View()
        for i, btn in enumerate(entry.get("buttons", [])[:5]):
            btn_url = btn.get("url", "")
            if btn_url.startswith("http://") or btn_url.startswith("https://"):
                if len(btn_url) > 8:
                    view.add_item(discord.ui.Button(
                        label=btn.get("label", "Button"), url=btn_url,
                        style=BUTTON_COLORS[i % len(BUTTON_COLORS)],
                    ))
        delete_after = entry.get("delete_after") or 0
        sent = await message.channel.send(embed=embed, view=view)
        if delete_after:
            await asyncio.sleep(delete_after)
            try:
                await sent.delete()
            except Exception:
                pass
        return


# ── Slash commands ────────────────────────────────────────────────────────────

autoreply_group = app_commands.Group(name="autoreply", description="Manage keyword auto replies")


@autoreply_group.command(name="add", description="Add a keyword auto reply (up to 5 channels + 5 categories)")
@app_commands.describe(
    channel1="Channel to watch", channel2="2nd channel (optional)", channel3="3rd channel (optional)",
    channel4="4th channel (optional)", channel5="5th channel (optional)",
    category1="Category to watch", category2="2nd category (optional)", category3="3rd category (optional)",
    category4="4th category (optional)", category5="5th category (optional)",
    delete_after="Auto-delete the reply after this many seconds (0 = never)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def autoreply_add(
    interaction: discord.Interaction,
    channel1: discord.TextChannel = None, channel2: discord.TextChannel = None,
    channel3: discord.TextChannel = None, channel4: discord.TextChannel = None,
    channel5: discord.TextChannel = None,
    category1: discord.CategoryChannel = None, category2: discord.CategoryChannel = None,
    category3: discord.CategoryChannel = None, category4: discord.CategoryChannel = None,
    category5: discord.CategoryChannel = None,
    delete_after: app_commands.Range[int, 0, 86400] = 0,
):
    targets = []
    for ch in [channel1, channel2, channel3, channel4, channel5]:
        if ch:
            targets.append({"id": ch.id, "name": ch.name, "is_category": False})
    for cat in [category1, category2, category3, category4, category5]:
        if cat:
            targets.append({"id": cat.id, "name": cat.name, "is_category": True})
    if not targets:
        await interaction.response.send_message("❌ Please provide at least one channel or category.", ephemeral=True)
        return
    await interaction.response.send_modal(AutoReplyModal(targets, delete_after))


@autoreply_group.command(name="edit", description="Edit an existing auto reply rule")
@app_commands.describe(keyword="Any keyword from the rule you want to edit", delete_after="Auto-delete delay in seconds")
@app_commands.checks.has_permissions(manage_guild=True)
async def autoreply_edit(
    interaction: discord.Interaction, keyword: str,
    channel1: discord.TextChannel = None, channel2: discord.TextChannel = None,
    channel3: discord.TextChannel = None, channel4: discord.TextChannel = None,
    channel5: discord.TextChannel = None,
    category1: discord.CategoryChannel = None, category2: discord.CategoryChannel = None,
    category3: discord.CategoryChannel = None, category4: discord.CategoryChannel = None,
    category5: discord.CategoryChannel = None,
    delete_after: app_commands.Range[int, 0, 86400] = None,
):
    kw = keyword.strip().lower()
    autoreplies = load_autoreplies()
    entry_index = next((i for i, e in enumerate(autoreplies) if kw in e.get("keywords", [])), None)
    if entry_index is None:
        await interaction.response.send_message(f"❌ No rule found containing keyword `{keyword}`.", ephemeral=True)
        return
    provided = [channel1, channel2, channel3, channel4, channel5, category1, category2, category3, category4, category5]
    new_targets = None
    if any(p is not None for p in provided):
        new_targets = []
        for ch in [channel1, channel2, channel3, channel4, channel5]:
            if ch:
                new_targets.append({"id": ch.id, "name": ch.name, "is_category": False})
        for cat in [category1, category2, category3, category4, category5]:
            if cat:
                new_targets.append({"id": cat.id, "name": cat.name, "is_category": True})
    await interaction.response.send_modal(EditAutoReplyModal(entry_index, autoreplies[entry_index], new_targets, delete_after))


@autoreply_group.command(name="setdelay", description="Set or clear the auto-delete delay on an existing rule")
@app_commands.describe(keyword="Any keyword from the rule to update", delete_after="Seconds before reply is deleted (0 = never)")
@app_commands.checks.has_permissions(manage_guild=True)
async def autoreply_setdelay(interaction: discord.Interaction, keyword: str, delete_after: app_commands.Range[int, 0, 86400]):
    kw = keyword.strip().lower()
    autoreplies = load_autoreplies()
    idx = next((i for i, e in enumerate(autoreplies) if kw in e.get("keywords", [])), None)
    if idx is None:
        await interaction.response.send_message(f"❌ No rule found containing keyword `{keyword}`.", ephemeral=True)
        return
    autoreplies[idx]["delete_after"] = delete_after
    save_autoreplies(autoreplies)
    msg = f"✅ Rule `{keyword}` will auto-delete after **{delete_after}s**." if delete_after else f"✅ Auto-delete disabled for rule `{keyword}`."
    await interaction.response.send_message(msg, ephemeral=True)


@autoreply_group.command(name="edittargets", description="Change which channels/categories a rule applies to")
@app_commands.describe(keyword="Any keyword from the rule you want to update")
@app_commands.checks.has_permissions(manage_guild=True)
async def autoreply_edittargets(
    interaction: discord.Interaction, keyword: str,
    channel1: discord.TextChannel = None, channel2: discord.TextChannel = None,
    channel3: discord.TextChannel = None, channel4: discord.TextChannel = None,
    channel5: discord.TextChannel = None,
    category1: discord.CategoryChannel = None, category2: discord.CategoryChannel = None,
    category3: discord.CategoryChannel = None, category4: discord.CategoryChannel = None,
    category5: discord.CategoryChannel = None,
):
    kw = keyword.strip().lower()
    autoreplies = load_autoreplies()
    idx = next((i for i, e in enumerate(autoreplies) if kw in e.get("keywords", [])), None)
    if idx is None:
        await interaction.response.send_message(f"❌ No rule found containing keyword `{keyword}`.", ephemeral=True)
        return
    new_targets = []
    for ch in [channel1, channel2, channel3, channel4, channel5]:
        if ch:
            new_targets.append({"id": ch.id, "name": ch.name, "is_category": False})
    for cat in [category1, category2, category3, category4, category5]:
        if cat:
            new_targets.append({"id": cat.id, "name": cat.name, "is_category": True})
    if not new_targets:
        await interaction.response.send_message("❌ Please provide at least one channel or category.", ephemeral=True)
        return
    old_targets = autoreplies[idx].get("targets", [])
    autoreplies[idx]["targets"] = new_targets
    save_autoreplies(autoreplies)
    await interaction.response.send_message(
        f"✅ Targets updated for rule `{keyword}`!\n"
        f"**Before:** {', '.join(['**' + _t_name(t) + '**' for t in old_targets]) or 'None'}\n"
        f"**After:** {', '.join(['**' + _t_name(t) + '**' for t in new_targets])}",
        ephemeral=True,
    )


@autoreply_group.command(name="remove", description="Remove an auto reply rule by any one of its keywords")
@app_commands.describe(keyword="Any one of the trigger keywords in the rule to remove")
@app_commands.checks.has_permissions(manage_guild=True)
async def autoreply_remove(interaction: discord.Interaction, keyword: str):
    kw = keyword.strip().lower()
    autoreplies = load_autoreplies()
    new_list = [e for e in autoreplies if kw not in e.get("keywords", [])]
    if len(new_list) == len(autoreplies):
        await interaction.response.send_message(f"❌ No rule found containing keyword `{keyword}`.", ephemeral=True)
        return
    save_autoreplies(new_list)
    await interaction.response.send_message(f"✅ Removed auto reply rule containing keyword `{keyword}`.", ephemeral=True)


@autoreply_group.command(name="list", description="List all active auto reply rules")
@app_commands.checks.has_permissions(manage_guild=True)
async def autoreply_list(interaction: discord.Interaction):
    autoreplies = load_autoreplies()
    if not autoreplies:
        await interaction.response.send_message("📭 No auto reply rules configured yet.", ephemeral=True)
        return
    embed = discord.Embed(title="Auto Reply Rules", color=discord.Color.blurple(), timestamp=datetime.utcnow())
    for i, entry in enumerate(autoreplies, 1):
        kws = ", ".join(f"`{k}`" for k in entry.get("keywords", []))
        target_lines = [("📁" if _t_is_cat(t) else "💬") + f" **{_t_name(t)}**" for t in entry.get("targets", [])]
        delay = entry.get("delete_after", 0)
        delay_line = f"\n🕒 Deletes after **{delay}s**" if delay else ""
        embed.add_field(name=f"{i}. {kws}", value=("\n".join(target_lines) or "No targets") + delay_line, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: discord.ext.commands.Bot):
    bot.tree.add_command(autoreply_group)
    bot.add_listener(handle_autoreply, "on_message")
