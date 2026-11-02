"""
modules/trading.py
Trading channels with private trade threads and +rep / -rep.

Flow: a member posts in a configured trading channel -> the bot reacts with the trade emoji ->
another member clicks it -> the bot opens a private thread for the two of them (plus the configured
staff roles). Inside the thread both traders confirm the trade is done, then each one can vouch (+rep)
or downvote (-rep) the other. Reputation is stored per server.

Data (database/*.json):
  trading_settings    guild_id -> settings
  trading_threads     thread_id -> trade record
  trading_reputation  guild_id -> user_id -> {"up": n, "down": n}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from modules.utils import load_json, save_json

_bot: commands.Bot | None = None
SETTINGS_FILE = "trading_settings"
THREADS_FILE = "trading_threads"
REP_FILE = "trading_reputation"

DEFAULT_EMOJI = "🤝"
MAX_THREAD_NAME = 90

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "channel_ids": [],
    "staff_role_ids": [],
    "stats_channel_ids": [],  # where /trading stats and /trading top work; empty = anywhere
    "emoji": DEFAULT_EMOJI,
}


# ── Storage ──────────────────────────────────────────────────────────────────

def get_settings(guild_id: int | str) -> dict[str, Any]:
    stored = load_json(SETTINGS_FILE, {}).get(str(guild_id), {})
    merged = dict(DEFAULT_SETTINGS)
    if isinstance(stored, dict):
        merged.update(stored)
    return merged


def save_settings(guild_id: int | str, settings: dict[str, Any]) -> None:
    data = load_json(SETTINGS_FILE, {})
    data[str(guild_id)] = settings
    save_json(SETTINGS_FILE, data)


def get_reputation(guild_id: int | str, user_id: int | str) -> dict[str, int]:
    rec = load_json(REP_FILE, {}).get(str(guild_id), {}).get(str(user_id), {})
    return {"up": int(rec.get("up", 0)), "down": int(rec.get("down", 0)), "trades": int(rec.get("trades", 0))}


def _add_reputation(guild_id: int, user_id: int, vote: str, name: str = "") -> None:
    data = load_json(REP_FILE, {})
    rec = data.setdefault(str(guild_id), {}).setdefault(str(user_id), {"up": 0, "down": 0, "trades": 0})
    rec[vote] = int(rec.get(vote, 0)) + 1
    if name:
        rec["name"] = name  # lets the dashboard show names without a Discord lookup
    save_json(REP_FILE, data)


def _add_successful_trade(guild_id: int, users: list[tuple[int, str]]) -> None:
    data = load_json(REP_FILE, {})
    guild = data.setdefault(str(guild_id), {})
    for uid, name in users:
        rec = guild.setdefault(str(uid), {"up": 0, "down": 0, "trades": 0})
        if name:
            rec["name"] = name
        rec["trades"] = int(rec.get("trades", 0)) + 1
    save_json(REP_FILE, data)


def _get_trade(thread_id: int) -> dict[str, Any] | None:
    return load_json(THREADS_FILE, {}).get(str(thread_id))


def _save_trade(thread_id: int, trade: dict[str, Any]) -> None:
    data = load_json(THREADS_FILE, {})
    data[str(thread_id)] = trade
    save_json(THREADS_FILE, data)


def _listing_has_trade(message_id: int) -> bool:
    return any(
        t.get("message_id") == str(message_id) and t.get("status") != "cancelled"
        for t in load_json(THREADS_FILE, {}).values()
    )


def _is_staff(member: discord.abc.User, settings: dict[str, Any]) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild or member.guild_permissions.manage_threads:
        return True
    staff = {str(r) for r in settings.get("staff_role_ids", [])}
    return any(str(r.id) in staff for r in member.roles)


def _rep_text(guild_id: int, user_id: int) -> str:
    rep = get_reputation(guild_id, user_id)
    return (f"👍 {rep['up']}  ·  👎 {rep['down']}  ·  Score **{rep['up'] - rep['down']:+d}**"
            f"  ·  🤝 {rep['trades']} trades")


def guild_stats(guild_id: int | str) -> dict[str, Any]:
    """Totals and leaderboards for the dashboard."""
    records = load_json(REP_FILE, {}).get(str(guild_id), {})
    rows = [
        {"user_id": uid, "name": r.get("name", ""), "up": int(r.get("up", 0)),
         "down": int(r.get("down", 0)), "trades": int(r.get("trades", 0))}
        for uid, r in records.items()
    ]
    threads = [t for t in load_json(THREADS_FILE, {}).values() if str(t.get("guild_id")) == str(guild_id)]
    return {
        "total_trades": sum(1 for t in threads if t.get("status") == "completed"),
        "open_trades": sum(1 for t in threads if t.get("status") == "open"),
        "total_vouches": sum(r["up"] for r in rows),
        "top_vouches": sorted((r for r in rows if r["up"]), key=lambda r: r["up"], reverse=True)[:10],
        "top_trades": sorted((r for r in rows if r["trades"]), key=lambda r: r["trades"], reverse=True)[:10],
    }


# ── Thread controls ──────────────────────────────────────────────────────────

async def _can_use(interaction: discord.Interaction, trade: dict[str, Any] | None) -> bool:
    if trade is None:
        await interaction.response.send_message("❌ This trade is no longer tracked.", ephemeral=True)
        return False
    if interaction.user.id in (int(trade["owner_id"]), int(trade["partner_id"])):
        return True
    if _is_staff(interaction.user, get_settings(interaction.guild_id)):
        return True
    await interaction.response.send_message("❌ Only the two traders and staff can use this.", ephemeral=True)
    return False


class TradeControlView(discord.ui.View):
    """Complete / close buttons shown at the top of every trade thread."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Complete Trade", style=discord.ButtonStyle.success, emoji="✅",
                       custom_id="trading:complete")
    async def complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade = _get_trade(interaction.channel_id)
        if not await _can_use(interaction, trade):
            return
        if trade["status"] != "open":
            await interaction.response.send_message("This trade is already finished.", ephemeral=True)
            return

        settings = get_settings(interaction.guild_id)
        staff = _is_staff(interaction.user, settings)
        parties = {int(trade["owner_id"]), int(trade["partner_id"])}
        confirmed = set(trade.get("confirmed", []))
        if interaction.user.id in parties:
            confirmed.add(interaction.user.id)
        trade["confirmed"] = sorted(confirmed)

        if not (staff and interaction.user.id not in parties) and confirmed != parties:
            _save_trade(interaction.channel_id, trade)
            await interaction.response.send_message(
                f"✅ {interaction.user.mention} confirmed the trade is done. "
                "Waiting for the other trader to confirm too."
            )
            return

        trade["status"] = "completed"
        trade["completed_at"] = datetime.now(timezone.utc).isoformat()
        _save_trade(interaction.channel_id, trade)
        _add_successful_trade(interaction.guild_id, [
            (int(uid), m.display_name if (m := interaction.guild.get_member(int(uid))) else "")
            for uid in (trade["owner_id"], trade["partner_id"])
        ])

        embed = discord.Embed(
            title="🎉 Trade completed",
            description=(
                f"<@{trade['owner_id']}> and <@{trade['partner_id']}>, how did it go?\n"
                "Rate **the other trader** with the buttons below. Each of you can vote once."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, view=RatingView())

    @discord.ui.button(label="Report Scam", style=discord.ButtonStyle.secondary, emoji="🚨",
                       custom_id="trading:scam")
    async def report_scam(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade = _get_trade(interaction.channel_id)
        if not await _can_use(interaction, trade):
            return
        if interaction.user.id not in (int(trade["owner_id"]), int(trade["partner_id"])):
            await interaction.response.send_message("❌ Only the two traders can report a scam.", ephemeral=True)
            return
        reports = trade.setdefault("scam_reports", [])
        if interaction.user.id in reports:
            await interaction.response.send_message("You already reported this trade. Staff were called.", ephemeral=True)
            return
        reports.append(interaction.user.id)
        _save_trade(interaction.channel_id, trade)

        other = int(trade["partner_id"]) if interaction.user.id == int(trade["owner_id"]) else int(trade["owner_id"])
        staff = " ".join(f"<@&{r}>" for r in get_settings(interaction.guild_id)["staff_role_ids"])
        await interaction.response.send_message(
            f"🚨 {staff}\n{interaction.user.mention} reported a possible **scam** by <@{other}>. "
            "Staff, please review this thread. Traders: post your proof here and do not complete the trade.",
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )

    @discord.ui.button(label="Close Trade", style=discord.ButtonStyle.danger, emoji="🔒",
                       custom_id="trading:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade = _get_trade(interaction.channel_id)
        if not await _can_use(interaction, trade):
            return
        if trade["status"] == "open":
            trade["status"] = "cancelled"
            _save_trade(interaction.channel_id, trade)
            # Let the listing be picked up again.
            try:
                channel = interaction.guild.get_channel(int(trade["channel_id"]))
                msg = await channel.fetch_message(int(trade["message_id"]))
                await msg.add_reaction(get_settings(interaction.guild_id)["emoji"])
            except Exception:
                pass
        await interaction.response.send_message(f"🔒 Trade closed by {interaction.user.mention}.")
        thread = interaction.channel
        if isinstance(thread, discord.Thread) and trade["status"] == "cancelled":
            try:
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException:
                pass


class RatingView(discord.ui.View):
    """Vouch / downvote buttons shown once a trade is completed."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _vote(self, interaction: discord.Interaction, vote: str):
        trade = _get_trade(interaction.channel_id)
        if trade is None or trade.get("status") != "completed":
            await interaction.response.send_message("❌ This trade is not completed.", ephemeral=True)
            return
        owner, partner = int(trade["owner_id"]), int(trade["partner_id"])
        if interaction.user.id not in (owner, partner):
            await interaction.response.send_message("❌ Only the two traders can rate.", ephemeral=True)
            return
        votes = trade.setdefault("votes", {})
        if str(interaction.user.id) in votes:
            await interaction.response.send_message("You already rated this trade.", ephemeral=True)
            return

        target = partner if interaction.user.id == owner else owner
        votes[str(interaction.user.id)] = vote
        _save_trade(interaction.channel_id, trade)
        target_member = interaction.guild.get_member(target)
        _add_reputation(interaction.guild_id, target, vote, target_member.display_name if target_member else "")

        sign = "+1 rep 👍" if vote == "up" else "-1 rep 👎"
        await interaction.response.send_message(
            f"{interaction.user.mention} rated <@{target}>: **{sign}**\n"
            f"<@{target}> now has {_rep_text(interaction.guild_id, target)}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

        if len(votes) >= 2:
            thread = interaction.channel
            try:
                await interaction.message.edit(view=None)
                await thread.send("✅ Both traders rated. This thread is now archived.")
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Vouch (+rep)", style=discord.ButtonStyle.success, emoji="👍",
                       custom_id="trading:up")
    async def up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "up")

    @discord.ui.button(label="Downvote (-rep)", style=discord.ButtonStyle.secondary, emoji="👎",
                       custom_id="trading:down")
    async def down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "down")


# ── Listeners ────────────────────────────────────────────────────────────────

async def _on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    settings = get_settings(message.guild.id)
    if not settings["enabled"] or str(message.channel.id) not in {str(c) for c in settings["channel_ids"]}:
        return
    if isinstance(message.channel, discord.Thread):
        return
    try:
        await message.add_reaction(settings["emoji"])
    except discord.HTTPException:
        pass


async def _on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if _bot is None or payload.guild_id is None or payload.user_id == _bot.user.id:
        return
    settings = get_settings(payload.guild_id)
    if not settings["enabled"] or str(payload.channel_id) not in {str(c) for c in settings["channel_ids"]}:
        return
    if str(payload.emoji) != settings["emoji"]:
        return

    guild = _bot.get_guild(payload.guild_id)
    channel = guild.get_channel(payload.channel_id) if guild else None
    if channel is None or isinstance(channel, discord.Thread):
        return

    async def _remove_reaction():
        try:
            member = payload.member or guild.get_member(payload.user_id)
            msg = await channel.fetch_message(payload.message_id)
            await msg.remove_reaction(payload.emoji, member)
        except Exception:
            pass

    if payload.member is not None and payload.member.bot:
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    if message.author.bot:
        return

    if message.author.id == payload.user_id:
        await _remove_reaction()  # can't trade with yourself
        return
    if _listing_has_trade(message.id):
        await _remove_reaction()  # someone already took this listing
        return

    partner = payload.member or guild.get_member(payload.user_id)
    if partner is None:
        return

    name = f"trade-{message.author.display_name}-{partner.display_name}"[:MAX_THREAD_NAME]
    try:
        thread = await channel.create_thread(
            name=name, type=discord.ChannelType.private_thread, invitable=False,
            reason=f"Trade between {message.author} and {partner}",
        )
        await thread.add_user(message.author)
        await thread.add_user(partner)
    except discord.HTTPException as exc:
        await _remove_reaction()
        print(f"[Trading] could not create thread in #{channel.name}: {exc}")
        return

    _save_trade(thread.id, {
        "guild_id": str(guild.id),
        "channel_id": str(channel.id),
        "message_id": str(message.id),
        "owner_id": str(message.author.id),
        "partner_id": str(partner.id),
        "status": "open",
        "confirmed": [],
        "votes": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    # Mentioning a staff role is what pulls its members into a private thread.
    staff_mentions = " ".join(f"<@&{r}>" for r in settings["staff_role_ids"])
    snippet = (message.content or "*(attachment)*")[:500]
    embed = discord.Embed(
        title="🤝 Trade thread",
        description=(
            f"{message.author.mention} ⇄ {partner.mention}\n\n"
            f"**Listing:**\n{snippet}\n\n"
            "Discuss and complete your trade here."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name=message.author.display_name, value=_rep_text(guild.id, message.author.id), inline=True)
    embed.add_field(name=partner.display_name, value=_rep_text(guild.id, partner.id), inline=True)
    embed.add_field(
        name="✅ If the trade was successful",
        value=(
            "**Both** of you press **Complete Trade**, then press **👍 Vouch (+rep)** for the other trader "
            "(or 👎 if it went badly). You can also post a written review with `/vouch`, "
            "and check anyone's record with `/trading stats`."
        ),
        inline=False,
    )
    embed.add_field(
        name="🚨 If it was a scam",
        value=(
            "Do **not** press Complete Trade. Press **Report Scam** and staff will be called. "
            "Keep your screenshots and proof in this thread."
        ),
        inline=False,
    )
    embed.set_footer(text="Never trade outside this thread. Staff can see this conversation.")
    await thread.send(
        content=f"{message.author.mention} {partner.mention} {staff_mentions}".strip(),
        embed=embed, view=TradeControlView(),
        allowed_mentions=discord.AllowedMentions(users=True, roles=True),
    )

    try:
        await message.reply(
            f"🤝 {partner.mention} wants to trade! Continue in {thread.mention}.",
            mention_author=True,
        )
        await message.clear_reaction(payload.emoji)
    except discord.HTTPException:
        pass


# ── Slash commands ───────────────────────────────────────────────────────────

trading_group = app_commands.Group(name="trading", description="Trading stats and leaderboards")
tradeadmin_group = app_commands.Group(
    name="tradeadmin", description="Configure trading channels and staff",
    default_permissions=discord.Permissions(manage_guild=True),
)


@tradeadmin_group.command(name="setup", description="Make a channel a trading channel")
@app_commands.describe(channel="Channel where members post trade offers", emoji="Reaction used to start a trade")
@app_commands.checks.has_permissions(manage_guild=True)
async def trading_setup(interaction: discord.Interaction, channel: discord.TextChannel, emoji: str | None = None):
    settings = get_settings(interaction.guild_id)
    ids = {str(c) for c in settings["channel_ids"]}
    ids.add(str(channel.id))
    settings["channel_ids"] = sorted(ids)
    settings["enabled"] = True
    if emoji:
        settings["emoji"] = emoji.strip()
    save_settings(interaction.guild_id, settings)

    me = channel.permissions_for(interaction.guild.me)
    missing = [n for n, ok in (
        ("Add Reactions", me.add_reactions), ("Create Private Threads", me.create_private_threads),
        ("Send Messages in Threads", me.send_messages_in_threads), ("Manage Messages", me.manage_messages),
        ("Read Message History", me.read_message_history),
    ) if not ok]
    note = f"\n⚠️ I'm missing in that channel: **{', '.join(missing)}**" if missing else ""
    await interaction.response.send_message(
        f"✅ {channel.mention} is now a trading channel. Trade emoji: {settings['emoji']}{note}", ephemeral=True
    )


@tradeadmin_group.command(name="remove", description="Stop treating a channel as a trading channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def trading_remove(interaction: discord.Interaction, channel: discord.TextChannel):
    settings = get_settings(interaction.guild_id)
    settings["channel_ids"] = [c for c in settings["channel_ids"] if str(c) != str(channel.id)]
    save_settings(interaction.guild_id, settings)
    await interaction.response.send_message(f"🗑️ {channel.mention} removed from trading channels.", ephemeral=True)


@tradeadmin_group.command(name="staff_add", description="Add a staff role (moderator, helper, support...) to every trade thread")
@app_commands.checks.has_permissions(manage_guild=True)
async def trading_staff_add(interaction: discord.Interaction, role: discord.Role):
    settings = get_settings(interaction.guild_id)
    ids = {str(r) for r in settings["staff_role_ids"]}
    ids.add(str(role.id))
    settings["staff_role_ids"] = sorted(ids)
    save_settings(interaction.guild_id, settings)
    warn = "" if role.mentionable or interaction.guild.me.guild_permissions.mention_everyone else (
        "\n⚠️ This role isn't mentionable and I lack **Mention Everyone**, so I can't pull it into threads. "
        "Make the role mentionable or give me that permission."
    )
    await interaction.response.send_message(f"✅ {role.mention} will be added to trade threads.{warn}", ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


@tradeadmin_group.command(name="staff_remove", description="Remove a staff role from trade threads")
@app_commands.checks.has_permissions(manage_guild=True)
async def trading_staff_remove(interaction: discord.Interaction, role: discord.Role):
    settings = get_settings(interaction.guild_id)
    settings["staff_role_ids"] = [r for r in settings["staff_role_ids"] if str(r) != str(role.id)]
    save_settings(interaction.guild_id, settings)
    await interaction.response.send_message(f"🗑️ {role.mention} removed.", ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


@tradeadmin_group.command(name="toggle", description="Enable or disable trading in this server")
@app_commands.checks.has_permissions(manage_guild=True)
async def trading_toggle(interaction: discord.Interaction):
    settings = get_settings(interaction.guild_id)
    settings["enabled"] = not settings["enabled"]
    save_settings(interaction.guild_id, settings)
    await interaction.response.send_message(
        f"Trading is now **{'enabled' if settings['enabled'] else 'disabled'}**.", ephemeral=True
    )


@tradeadmin_group.command(name="status", description="Show the trading configuration")
@app_commands.checks.has_permissions(manage_guild=True)
async def trading_status(interaction: discord.Interaction):
    s = get_settings(interaction.guild_id)
    channels = ", ".join(f"<#{c}>" for c in s["channel_ids"]) or "none"
    roles = ", ".join(f"<@&{r}>" for r in s["staff_role_ids"]) or "none"
    stats = ", ".join(f"<#{c}>" for c in s["stats_channel_ids"]) or "anywhere"
    await interaction.response.send_message(
        f"**Trading** — {'enabled' if s['enabled'] else 'disabled'}\n"
        f"Channels: {channels}\nStats channels: {stats}\nEmoji: {s['emoji']}\nStaff roles: {roles}",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


class ChannelPickerView(discord.ui.View):
    """Two multi-selects: trading channels and stats channels. Each saves as soon as it changes."""

    def __init__(self, guild_id: int, settings: dict[str, Any]):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.trading = self._select("Select trading channels…", settings["channel_ids"], "channel_ids")
        self.stats = self._select("Select stats channels (empty = anywhere)…",
                                  settings["stats_channel_ids"], "stats_channel_ids")

    def _select(self, placeholder: str, current: list[str], key: str) -> discord.ui.ChannelSelect:
        select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=placeholder, min_values=0, max_values=25,
            default_values=[discord.Object(id=int(c)) for c in current[:25] if str(c).isdigit()],
        )

        async def _picked(interaction: discord.Interaction):
            settings = get_settings(self.guild_id)
            settings[key] = [str(c.id) for c in select.values]
            if key == "channel_ids":
                settings["enabled"] = True
            save_settings(self.guild_id, settings)
            await interaction.response.send_message(
                f"✅ {'Trading' if key == 'channel_ids' else 'Stats'} channels: "
                + (", ".join(f"<#{c}>" for c in settings[key]) or "none"), ephemeral=True)

        select.callback = _picked
        self.add_item(select)
        return select


@tradeadmin_group.command(name="channels", description="Pick the trading channels and the stats channels (multi-select)")
@app_commands.checks.has_permissions(manage_guild=True)
async def trading_channels(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Row 1:** channels where members post trade offers.\n"
        "**Row 2:** channels where `/trading stats` and `/trading top` can be used.",
        view=ChannelPickerView(interaction.guild_id, get_settings(interaction.guild_id)), ephemeral=True,
    )


async def _stats_channel_ok(interaction: discord.Interaction) -> bool:
    allowed = [str(c) for c in get_settings(interaction.guild_id)["stats_channel_ids"]]
    if not allowed or str(interaction.channel_id) in allowed:
        return True
    await interaction.response.send_message(
        "❌ Use this command in: " + ", ".join(f"<#{c}>" for c in allowed), ephemeral=True)
    return False


@trading_group.command(name="stats", description="Show a member's trading stats (rep, vouches, successful trades)")
async def trading_stats(interaction: discord.Interaction, member: discord.Member | None = None):
    if not await _stats_channel_ok(interaction):
        return
    member = member or interaction.user
    rep = get_reputation(interaction.guild_id, member.id)
    total_votes = rep["up"] + rep["down"]
    rate = f"{rep['up'] / total_votes * 100:.0f}%" if total_votes else "—"
    embed = discord.Embed(title=f"📊 Trading stats — {member.display_name}", color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="👍 Vouches (+rep)", value=str(rep["up"]), inline=True)
    embed.add_field(name="👎 Downvotes", value=str(rep["down"]), inline=True)
    embed.add_field(name="⭐ Rep score", value=f"{rep['up'] - rep['down']:+d}", inline=True)
    embed.add_field(name="🤝 Successful trades", value=str(rep["trades"]), inline=True)
    embed.add_field(name="💯 Positive rate", value=rate, inline=True)
    await interaction.response.send_message(embed=embed)


@trading_group.command(name="top", description="Leaderboard of top vouches or most successful trades")
@app_commands.choices(category=[
    app_commands.Choice(name="Top vouches (+rep)", value="up"),
    app_commands.Choice(name="Top successful trades", value="trades"),
])
async def trading_top(interaction: discord.Interaction, category: app_commands.Choice[str]):
    if not await _stats_channel_ok(interaction):
        return
    key = category.value
    records = load_json(REP_FILE, {}).get(str(interaction.guild_id), {})
    ranked = sorted(
        ((uid, int(r.get(key, 0))) for uid, r in records.items() if int(r.get(key, 0)) > 0),
        key=lambda x: x[1], reverse=True,
    )[:10]
    if not ranked:
        await interaction.response.send_message("Nobody has any yet.", ephemeral=True)
        return
    medals = ["🥇", "🥈", "🥉"]
    unit = "vouches" if key == "up" else "trades"
    lines = [f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{uid}> — **{n}** {unit}" for i, (uid, n) in enumerate(ranked)]
    embed = discord.Embed(title=f"🏆 {category.name}", description="\n".join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


# ── Registration ─────────────────────────────────────────────────────────────

def register(bot: commands.Bot):
    global _bot
    _bot = bot
    bot.tree.add_command(trading_group)
    bot.tree.add_command(tradeadmin_group)
    bot.add_view(TradeControlView())
    bot.add_view(RatingView())
    bot.add_listener(_on_message, "on_message")
    bot.add_listener(_on_raw_reaction_add, "on_raw_reaction_add")
