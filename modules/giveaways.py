"""
modules/giveaways.py
Complete Giveaway system – helpers, key delivery, core CRUD, background loop, slash commands.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import random
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from modules.utils import _as_int, load_json, save_json, GIVEAWAYS_FILE

# ── Bot reference (set by register()) ────────────────────────────────────────
_bot: discord.ext.commands.Bot | None = None

# ── Constants ─────────────────────────────────────────────────────────────────
GIVEAWAY_JOIN_EMOJI = "🎉"
GIVEAWAY_KEY_TIERS = {"none", "weekly", "monthly", "lifetime"}
GIVEAWAY_KEY_PRODUCTS = {
    "weekly": "Weekly Key",
    "monthly": "Monthly Key",
    "lifetime": "Lifetime Key",
}
GIVEAWAY_KEY_WEBHOOK_DEFAULTS = {
    "weekly": "https://api.jnkie.com/api/v1/webhooks/execute/595b693b-25c7-4500-a64f-49bb69261f0f",
    "monthly": "https://api.jnkie.com/api/v1/webhooks/execute/f50d91ce-67c3-4596-80be-93ed319d99a5",
    "lifetime": "https://api.jnkie.com/api/v1/webhooks/execute/08d8b944-d5a5-470b-a2a1-7462bf6b61b2",
}
GIVEAWAY_KEY_WEBHOOKS = {
    "weekly": str(os.getenv("GIVEAWAY_KEY_WEBHOOK_WEEKLY") or GIVEAWAY_KEY_WEBHOOK_DEFAULTS["weekly"]).strip(),
    "monthly": str(os.getenv("GIVEAWAY_KEY_WEBHOOK_MONTHLY") or GIVEAWAY_KEY_WEBHOOK_DEFAULTS["monthly"]).strip(),
    "lifetime": str(os.getenv("GIVEAWAY_KEY_WEBHOOK_LIFETIME") or GIVEAWAY_KEY_WEBHOOK_DEFAULTS["lifetime"]).strip(),
}

def _parse_id(val: any) -> int | None:
    try:
        return int(str(val or "").strip())
    except (ValueError, TypeError):
        return None

# OFFICIAL_GUILD_ID removed - configuration is now guild-agnostic via dashboard.


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _giveaway_to_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_giveaway_key_tier(value) -> str:
    tier = str(value or "").strip().lower()
    return tier if tier in GIVEAWAY_KEY_TIERS else "none"


def _giveaway_key_tier_label(tier: str) -> str:
    normalized = _normalize_giveaway_key_tier(tier)
    return {"none": "None", "weekly": "Weekly", "monthly": "Monthly", "lifetime": "Lifetime"}.get(normalized, "None")


def _giveaway_clamp_winner_count(value) -> int:
    try:
        w = int(value)
    except (TypeError, ValueError):
        w = 1
    return max(1, min(25, w))


def _giveaway_clamp_duration_minutes(value) -> int:
    try:
        m = int(value)
    except (TypeError, ValueError):
        m = 60
    return max(1, min(10080, m))


def _giveaway_parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _giveaway_key_delivery_snapshot(raw) -> dict:
    source = raw if isinstance(raw, dict) else {}
    failed_user_ids: list[int] = []
    for raw_uid in source.get("failed_user_ids", []):
        parsed = _giveaway_to_int(raw_uid)
        if parsed and parsed not in failed_user_ids:
            failed_user_ids.append(parsed)
    return {
        "tier": _normalize_giveaway_key_tier(source.get("tier")),
        "delivered_count": max(0, _giveaway_to_int(source.get("delivered_count")) or 0),
        "failed_count": max(0, _giveaway_to_int(source.get("failed_count")) or 0),
        "failed_user_ids": failed_user_ids,
        "last_delivery_at": str(source.get("last_delivery_at") or "") or None,
    }


def _resolve_giveaway_webhook_url(tier: str) -> str:
    normalized = _normalize_giveaway_key_tier(tier)
    if normalized == "none":
        return ""
    env_key = f"GIVEAWAY_KEY_WEBHOOK_{normalized.upper()}"
    return str(
        os.getenv(env_key)
        or GIVEAWAY_KEY_WEBHOOKS.get(normalized)
        or GIVEAWAY_KEY_WEBHOOK_DEFAULTS.get(normalized)
        or ""
    ).strip()


def _safe_ticket_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "-", str(text or "").lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "winner"


def _giveaway_pick_winners(
    entries: list[int], winner_count: int, excluded: set[int] | None = None
) -> list[int]:
    exclude = excluded or set()
    pool = [uid for uid in entries if uid not in exclude]
    random.shuffle(pool)
    if not pool and exclude:
        pool = list(entries)
        random.shuffle(pool)
    return pool[: min(_giveaway_clamp_winner_count(winner_count), len(pool))]


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_giveaways() -> dict:
    raw = load_json(GIVEAWAYS_FILE)
    if not isinstance(raw, dict):
        return {}

    normalized: dict = {}
    for guild_id, guild_entries in raw.items():
        guild_key = str(guild_id)
        if isinstance(guild_entries, dict):
            iterable = list(guild_entries.items())
        elif isinstance(guild_entries, list):
            iterable = []
            for entry in guild_entries:
                if not isinstance(entry, dict):
                    continue
                message_id = _giveaway_to_int(entry.get("message_id"))
                if not message_id:
                    continue
                iterable.append((str(message_id), entry))
        else:
            continue

        guild_normalized: dict = {}
        for message_key, entry in iterable:
            if not isinstance(entry, dict):
                continue
            message_id = _giveaway_to_int(entry.get("message_id") or message_key)
            channel_id = _giveaway_to_int(entry.get("channel_id"))
            if not message_id or not channel_id:
                continue

            winners = [w for w in (_giveaway_to_int(u) for u in entry.get("winners", [])) if w]
            last_reroll_winners = [w for w in (_giveaway_to_int(u) for u in entry.get("last_reroll_winners", [])) if w]
            entries_snapshot = [w for w in (_giveaway_to_int(u) for u in entry.get("entries_snapshot", [])) if w]
            awarded_user_ids = [w for w in (_giveaway_to_int(u) for u in entry.get("awarded_user_ids", [])) if w]
            key_tier = _normalize_giveaway_key_tier(entry.get("key_tier"))
            last_reroll_winner_count = _giveaway_to_int(entry.get("last_reroll_winner_count"))
            if last_reroll_winner_count is not None:
                last_reroll_winner_count = _giveaway_clamp_winner_count(last_reroll_winner_count)

            guild_normalized[str(message_id)] = {
                "guild_id": _giveaway_to_int(entry.get("guild_id")) or _giveaway_to_int(guild_key) or 0,
                "channel_id": channel_id,
                "message_id": message_id,
                "host_id": _giveaway_to_int(entry.get("host_id")),
                "reward_title": str(entry.get("reward_title") or "Mystery Reward").strip() or "Mystery Reward",
                "reward_description": str(entry.get("reward_description") or "").strip(),
                "winner_count": _giveaway_clamp_winner_count(entry.get("winner_count")),
                "duration_minutes": _giveaway_clamp_duration_minutes(entry.get("duration_minutes")),
                "ping_role_id": _giveaway_to_int(entry.get("ping_role_id")),
                "key_log_channel_id": _giveaway_to_int(entry.get("key_log_channel_id")),
                "emoji": str(entry.get("emoji") or GIVEAWAY_JOIN_EMOJI).strip() or GIVEAWAY_JOIN_EMOJI,
                "created_at": str(entry.get("created_at") or ""),
                "end_at": str(entry.get("end_at") or ""),
                "ended": bool(entry.get("ended", False)),
                "ended_at": str(entry.get("ended_at") or "") or None,
                "ended_by": _giveaway_to_int(entry.get("ended_by")),
                "ended_reason": str(entry.get("ended_reason") or ""),
                "entry_count": max(0, _giveaway_to_int(entry.get("entry_count")) or 0),
                "winners": winners,
                "jump_url": str(entry.get("jump_url") or "").strip() or None,
                "reroll_count": max(0, _giveaway_to_int(entry.get("reroll_count")) or 0),
                "last_rerolled_at": str(entry.get("last_rerolled_at") or "") or None,
                "last_rerolled_by": _giveaway_to_int(entry.get("last_rerolled_by")),
                "last_reroll_winners": last_reroll_winners,
                "last_reroll_winner_count": last_reroll_winner_count,
                "entries_snapshot": entries_snapshot,
                "key_tier": key_tier,
                "key_delivery": _giveaway_key_delivery_snapshot(entry.get("key_delivery")),
                "awarded_user_ids": awarded_user_ids,
            }

        normalized[guild_key] = guild_normalized

    return normalized


def save_giveaways(data: dict):
    save_json(GIVEAWAYS_FILE, data)


def _serialize_giveaway_entry(entry: dict) -> dict:
    def to_str_id(value) -> str | None:
        parsed = _giveaway_to_int(value)
        return str(parsed) if parsed else None

    key_tier = _normalize_giveaway_key_tier(entry.get("key_tier"))
    return {
        "guild_id": to_str_id(entry.get("guild_id")),
        "channel_id": to_str_id(entry.get("channel_id")),
        "message_id": to_str_id(entry.get("message_id")),
        "host_id": to_str_id(entry.get("host_id")),
        "reward_title": str(entry.get("reward_title") or ""),
        "reward_description": str(entry.get("reward_description") or ""),
        "winner_count": _giveaway_clamp_winner_count(entry.get("winner_count")),
        "duration_minutes": _giveaway_clamp_duration_minutes(entry.get("duration_minutes")),
        "ping_role_id": to_str_id(entry.get("ping_role_id")),
        "key_log_channel_id": to_str_id(entry.get("key_log_channel_id")),
        "emoji": str(entry.get("emoji") or GIVEAWAY_JOIN_EMOJI),
        "created_at": str(entry.get("created_at") or "") or None,
        "end_at": str(entry.get("end_at") or "") or None,
        "ended": bool(entry.get("ended", False)),
        "ended_at": str(entry.get("ended_at") or "") or None,
        "ended_by": to_str_id(entry.get("ended_by")),
        "ended_reason": str(entry.get("ended_reason") or "") or None,
        "entry_count": max(0, _giveaway_to_int(entry.get("entry_count")) or 0),
        "winners": [str(u) for u in entry.get("winners", []) if _giveaway_to_int(u)],
        "jump_url": str(entry.get("jump_url") or "") or None,
        "reroll_count": max(0, _giveaway_to_int(entry.get("reroll_count")) or 0),
        "last_rerolled_at": str(entry.get("last_rerolled_at") or "") or None,
        "last_rerolled_by": to_str_id(entry.get("last_rerolled_by")),
        "last_reroll_winners": [str(u) for u in entry.get("last_reroll_winners", []) if _giveaway_to_int(u)],
        "last_reroll_winner_count": _giveaway_to_int(entry.get("last_reroll_winner_count")),
        "entries_snapshot": [str(u) for u in entry.get("entries_snapshot", []) if _giveaway_to_int(u)],
        "key_tier": key_tier,
        "key_delivery": _giveaway_key_delivery_snapshot(entry.get("key_delivery")) if key_tier != "none" else None,
        "awarded_user_ids": [str(u) for u in entry.get("awarded_user_ids", []) if _giveaway_to_int(u)],
    }


# ── Embed builder ─────────────────────────────────────────────────────────────

def _giveaway_build_embed(entry: dict) -> discord.Embed:
    ended = bool(entry.get("ended", False))
    reward_title = str(entry.get("reward_title") or "Mystery Reward").strip() or "Mystery Reward"
    reward_description = str(entry.get("reward_description") or "").strip()
    winner_count = _giveaway_clamp_winner_count(entry.get("winner_count"))
    host_id = _giveaway_to_int(entry.get("host_id"))
    emoji = str(entry.get("emoji") or GIVEAWAY_JOIN_EMOJI).strip() or GIVEAWAY_JOIN_EMOJI
    key_tier = _normalize_giveaway_key_tier(entry.get("key_tier"))

    end_at_dt = _giveaway_parse_time(entry.get("end_at"))
    ended_at_dt = _giveaway_parse_time(entry.get("ended_at"))
    ts = ended_at_dt if (ended and ended_at_dt) else end_at_dt or datetime.now(timezone.utc)

    color = (discord.Color.green() if entry.get("winners") else discord.Color.red()) if ended else discord.Color.blurple()
    embed = discord.Embed(
        title="🎉 GIVEAWAY ENDED" if ended else "🎉 GIVEAWAY",
        description=reward_description[:4000] if reward_description else None,
        color=color,
        timestamp=ts,
    )
    embed.add_field(name="Reward", value=reward_title[:1024], inline=False)
    embed.add_field(name="Winners", value=str(winner_count), inline=True)
    if host_id:
        embed.add_field(name="Hosted by", value=f"<@{host_id}>", inline=True)
    if end_at_dt:
        unix_end = int(end_at_dt.timestamp())
        if ended:
            embed.add_field(name="Ended", value=f"<t:{unix_end}:F>", inline=True)
        else:
            embed.add_field(name="Ends", value=f"<t:{unix_end}:F>\n(<t:{unix_end}:R>)", inline=True)
    if key_tier != "none":
        key_label = _giveaway_key_tier_label(key_tier)
        if ended:
            delivery = _giveaway_key_delivery_snapshot(entry.get("key_delivery"))
            key_note = (
                f"{key_label} key reward\n"
                f"Delivered: {delivery['delivered_count']} | Failed: {delivery['failed_count']}"
            )
        else:
            key_note = f"{key_label} key reward will be delivered in a private key ticket channel."
        embed.add_field(name="Key Reward", value=key_note[:1024], inline=False)
    if not ended:
        embed.add_field(name="How to join", value=f"React with {emoji} below to enter.", inline=False)
    else:
        entry_count = max(0, _giveaway_to_int(entry.get("entry_count")) or 0)
        winners = [_giveaway_to_int(u) for u in entry.get("winners", [])]
        winners = [u for u in winners if u]
        if winners:
            embed.add_field(name="Winner(s)", value=", ".join(f"<@{u}>" for u in winners)[:1024], inline=False)
        else:
            embed.add_field(name="Winner(s)", value="No valid entries.", inline=False)
        embed.add_field(name="Entries", value=str(entry_count), inline=True)
        reroll_winners = [_giveaway_to_int(u) for u in entry.get("last_reroll_winners", [])]
        reroll_winners = [u for u in reroll_winners if u]
        if reroll_winners:
            embed.add_field(name="Latest reroll", value=", ".join(f"<@{u}>" for u in reroll_winners)[:1024], inline=False)

    message_id = _giveaway_to_int(entry.get("message_id"))
    embed.set_footer(text=f"Giveaway ID: {message_id}" if message_id else "Giveaway")
    return embed


# ── Async helpers ─────────────────────────────────────────────────────────────

async def _resolve_giveaway_log_channel(guild: discord.Guild, giveaway: dict):
    log_channel_id = _giveaway_to_int(giveaway.get("key_log_channel_id"))
    if not log_channel_id:
        return None
    channel = guild.get_channel(log_channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(log_channel_id)
        except Exception:
            return None
    return channel if hasattr(channel, "send") else None


async def _send_giveaway_key_log(
    log_channel,
    giveaway: dict,
    *,
    winner_user_id: int,
    tier: str,
    status_label: str,
    key_value: str | None = None,
    detail: str | None = None,
):
    if log_channel is None:
        return
    reward_title = str(giveaway.get("reward_title") or "Giveaway Reward")
    message_id = _giveaway_to_int(giveaway.get("message_id"))
    embed = discord.Embed(
        title="🔑 Giveaway Key Delivery Log",
        color=discord.Color.green() if status_label.lower().startswith("delivered") else discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Reward", value=reward_title[:1024], inline=False)
    embed.add_field(name="Winner", value=f"<@{winner_user_id}> (`{winner_user_id}`)", inline=True)
    embed.add_field(name="Tier", value=_giveaway_key_tier_label(tier), inline=True)
    embed.add_field(name="Status", value=status_label[:1024], inline=True)
    if message_id:
        embed.add_field(name="Giveaway ID", value=str(message_id), inline=True)
    jump_url = str(giveaway.get("jump_url") or "").strip()
    if jump_url:
        embed.add_field(name="Giveaway", value=f"[Jump to giveaway]({jump_url})", inline=False)
    if key_value:
        embed.add_field(name="Key", value=f"```{key_value[:1900]}```", inline=False)
    if detail:
        embed.add_field(name="Details", value=detail[:1024], inline=False)
    try:
        await log_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        pass


async def _fetch_giveaway_message(guild: discord.Guild, giveaway: dict):
    channel_id = _giveaway_to_int(giveaway.get("channel_id"))
    message_id = _giveaway_to_int(giveaway.get("message_id"))
    if not channel_id or not message_id:
        return None, None
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            return None, None
    if not hasattr(channel, "fetch_message"):
        return channel, None
    try:
        message = await channel.fetch_message(message_id)
    except Exception:
        return channel, None
    return channel, message


async def _collect_giveaway_entries(guild: discord.Guild, giveaway: dict) -> list[int]:
    if giveaway.get("ended"):
        frozen = []
        for uid in giveaway.get("entries_snapshot", []):
            u = _giveaway_to_int(uid)
            if u and u not in frozen:
                # Double check they are still in the guild even for rerolls
                if guild.get_member(u):
                    frozen.append(u)
        if frozen:
            return frozen


    _, message = await _fetch_giveaway_message(guild, giveaway)
    if message is None:
        return []

    emoji = str(giveaway.get("emoji") or GIVEAWAY_JOIN_EMOJI).strip() or GIVEAWAY_JOIN_EMOJI
    reaction = next((r for r in message.reactions if str(r.emoji) == emoji), None)
    if reaction is None and emoji != GIVEAWAY_JOIN_EMOJI:
        reaction = next((r for r in message.reactions if str(r.emoji) == GIVEAWAY_JOIN_EMOJI), None)
    if reaction is None:
        return []

    entries: list[int] = []
    async for user in reaction.users(limit=None):
        if user.bot:
            continue
        
        # Verify user is still a member of the guild to prevent cross-server or "ghost" winners
        if guild.get_member(user.id) is None:
            # Try to fetch if not in cache (sometimes cache is stale)
            try:
                if not await guild.fetch_member(user.id):
                    continue
            except Exception:
                continue

        if user.id not in entries:
            entries.append(user.id)
    return entries



async def _request_giveaway_key_from_webhook(key_tier: str) -> tuple[str | None, str | None]:
    tier = _normalize_giveaway_key_tier(key_tier)
    if tier == "none":
        return None, "No key tier configured."

    tier_webhook_url = _resolve_giveaway_webhook_url(tier)
    if not tier_webhook_url:
        return None, f"No webhook configured for tier '{tier}'."

    fallback_webhook_url = str(os.getenv("WEBHOOK_URL") or "").strip()
    webhook_secret = os.getenv("WEBHOOK_HMAC_SECRET")
    custom_hmac_header = str(os.getenv("WEBHOOK_HMAC_HEADER") or "seisen").strip()

    def _build_payload(product_name: str) -> dict:
        return {"item": {"product": {"name": product_name}, "quantity": 1}}

    def _build_headers(payload_bytes: bytes) -> dict:
        h = {"Content-Type": "application/json"}
        if webhook_secret:
            sig = hmac.new(webhook_secret.encode(), msg=payload_bytes, digestmod=hashlib.sha256).hexdigest()
            for key in ["X-Signature", "x-jnkie-signature", "signature", "jnkie-signature",
                        "Authorization", "X-Hub-Signature", "X-Hub-Signature-256",
                        "Discord-Boosting", "X-Discord-Boosting", "Discord-Boosting-Signature", "DiscordBoosting"]:
                h[key] = sig
            if custom_hmac_header:
                h[custom_hmac_header] = sig
        return h

    def _parse_key(text: str) -> str | None:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                keys = data.get("keys")
                if isinstance(keys, list) and keys:
                    return str(keys[0] or "").strip() or None
                if data.get("key"):
                    return str(data["key"]).strip() or None
            elif isinstance(data, list) and data:
                return str(data[0] or "").strip() or None
        except json.JSONDecodeError:
            stripped = text.strip()
            if stripped and not stripped.startswith("<"):
                return stripped
        return None

    async def _try_request(session, url: str, label: str, payload: dict):
        payload_bytes = json.dumps(payload).encode("utf-8")
        headers = _build_headers(payload_bytes)
        async with session.post(url, json=payload, headers=headers) as resp:
            text = await resp.text()
            if resp.status == 200:
                k = _parse_key(text)
                return (k, None) if k else (None, f"{label}: 200 OK without a key.")
            if resp.status == 204:
                return None, f"{label}: 204 No Content."
            short = text.strip()[:220] if text else ""
            return None, f"{label}: status {resp.status}" + (f" - {short}" if short else "")

    try:
        async with aiohttp.ClientSession() as session:
            combos = [
                (tier_webhook_url, f"Tier-{tier}", _build_payload(GIVEAWAY_KEY_PRODUCTS.get(tier, "Premium Key"))),
                (tier_webhook_url, f"Tier-{tier} (Premium)", _build_payload("Premium Key")),
            ]
            if fallback_webhook_url and fallback_webhook_url != tier_webhook_url:
                combos += [
                    (fallback_webhook_url, "Fallback", _build_payload(GIVEAWAY_KEY_PRODUCTS.get(tier, "Premium Key"))),
                    (fallback_webhook_url, "Fallback (Premium)", _build_payload("Premium Key")),
                ]
            errors = []
            for url, label, payload in combos:
                k, err = await _try_request(session, url, label, payload)
                if k:
                    return k, None
                errors.append(err or "Unknown error")
            return None, " | ".join(errors)
    except Exception as e:
        return None, f"Webhook request error: {e}"


# ── Claim Button View ─────────────────────────────────────────────────────────

class GiveawayKeyClaimView(discord.ui.View):
    def __init__(self, giveaway_id: int | None, channel_id: int, user_id: int):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        self.channel_id = channel_id
        self.user_id = user_id
    
    @discord.ui.button(label="✅ I have saved my key", style=discord.ButtonStyle.success, custom_id="claim_key_button")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Only allow the winner to click
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Only the winner can claim this key!", 
                ephemeral=True
            )
            return
        
        # Mark as claimed
        data = load_giveaways()
        
        giveaway_found = False
        guild_id_str = str(interaction.guild_id) if interaction.guild_id else None
        
        if guild_id_str and guild_id_str in data:
            giveaway = data[guild_id_str].get(str(self.giveaway_id))
            if giveaway:
                tracking = giveaway.get("key_claim_tracking", {})
                channel_key = str(self.channel_id)
                
                if channel_key in tracking:
                    tracking[channel_key]["claim_status"] = "claimed"
                    tracking[channel_key]["claim_confirmed_at"] = datetime.now(timezone.utc).isoformat()
                    giveaway_found = True
        
        if giveaway_found:
            save_giveaways(data)
        
        # Update the embed
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.green()
            embed.description = (
                f"✅ **Key Claimed Successfully!**\n\n"
                f"{interaction.user.mention}, you have confirmed that you saved your key.\n"
                f"This channel will remain open for your reference.\n\n"
                f"Thank you for participating in the giveaway!"
            )
        
        # Disable the button
        button.disabled = True
        button.label = "✅ Key Claimed"
        
        try:
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(
                "🎉 Thank you! Your key claim has been confirmed. You can close this channel anytime or keep it for reference.",
                ephemeral=False
            )
        except Exception as e:
            print(f"[Giveaway] Error updating claim message: {e}")


async def _create_giveaway_key_ticket(
    guild: discord.Guild,
    giveaway: dict,
    member: discord.Member,
    *,
    key_value: str,
    tier: str,
) -> tuple[discord.TextChannel | None, str | None]:
    from modules.boost import load_boost_config
    cfg = load_boost_config()
    guild_cfg = cfg.get(str(guild.id), {}) if isinstance(cfg, dict) else {}

    category = None
    category_id = _giveaway_to_int(guild_cfg.get("category_id"))
    if category_id:
        category = guild.get_channel(category_id)
        if category is None:
            try:
                category = await guild.fetch_channel(category_id)
            except Exception:
                category = None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
    }
    for raw_role_id in guild_cfg.get("roles", []):
        role_id = _giveaway_to_int(raw_role_id)
        if role_id:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    base = _safe_ticket_slug(member.display_name or member.name)
    channel_name = f"giveaway-key-{base}"[:95]
    if any(str(ch.name).lower() == channel_name.lower() for ch in guild.text_channels):
        channel_name = f"{channel_name[:86]}-{str(member.id)[-4:]}"

    try:
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites=overwrites,
            reason=f"Giveaway key delivery for winner {member.id}",
        )
    except Exception as e:
        return None, f"Failed to create key ticket channel: {e}"

    reward_title = str(giveaway.get("reward_title") or "Giveaway Reward")
    tier_label = _giveaway_key_tier_label(tier)
    jump_url = str(giveaway.get("jump_url") or "").strip()
    
    now_utc = datetime.now(timezone.utc)
    claim_deadline = now_utc + timedelta(minutes=10)
    claim_deadline_unix = int(claim_deadline.timestamp())

    embed = discord.Embed(
        title="🎁 Giveaway Winner Key",
        description=(
            f"Congratulations {member.mention}!\n"
            f"You won **{reward_title}** in **{guild.name}**.\n\n"
            f"⚠️ **IMPORTANT: You have 10 minutes to claim this key!**\n"
            f"Deadline: <t:{claim_deadline_unix}:R> (<t:{claim_deadline_unix}:T>)\n\n"
            f"**How to Claim:**\n"
            f"1️⃣ Copy the key below\n"
            f"2️⃣ Save it in a safe place\n"
            f"3️⃣ Click the **\"✅ I have saved my key\"** button\n\n"
            f"**If you do not claim within 10 minutes, this key will be voided and this channel will close.**"
        ),
        color=discord.Color.gold(),
        timestamp=now_utc,
    )
    embed.add_field(name="Tier", value=tier_label, inline=True)
    embed.add_field(name="Your Key", value=f"```{key_value}```", inline=False)
    if jump_url:
        embed.add_field(name="Giveaway", value=f"[Jump to giveaway]({jump_url})", inline=False)
    embed.set_footer(text="Keep this key private and do not share it.")
    
    # Create claim button view
    claim_view = GiveawayKeyClaimView(
        giveaway_id=giveaway.get("message_id"),
        channel_id=ticket_channel.id,
        user_id=member.id
    )

    try:
        sent_message = await ticket_channel.send(
            content=f"{member.mention} 🎉",
            embed=embed,
            view=claim_view,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
    except Exception as e:
        return None, f"Created key ticket but failed to send key message: {e}"
    
    # Initialize claim tracking in giveaway data
    if "key_claim_tracking" not in giveaway:
        giveaway["key_claim_tracking"] = {}
    
    giveaway["key_claim_tracking"][str(ticket_channel.id)] = {
        "user_id": member.id,
        "channel_id": ticket_channel.id,
        "message_id": sent_message.id,
        "claim_status": "pending",
        "claim_deadline_at": claim_deadline.isoformat(),
        "created_at": now_utc.isoformat(),
        "claim_confirmed_at": None,
        "reminder_sent": False,
        "key_value": key_value,
        "tier": tier,
    }

    return ticket_channel, None


async def _deliver_giveaway_keys(guild: discord.Guild, giveaway: dict, winner_ids: list[int]) -> dict:
    tier = _normalize_giveaway_key_tier(giveaway.get("key_tier"))
    if tier == "none" or not winner_ids:
        return {"tier": tier, "delivered_now": 0, "failed_now": 0, "failed_user_ids_now": []}

    previous = _giveaway_key_delivery_snapshot(giveaway.get("key_delivery"))
    awarded = {u for u in (_giveaway_to_int(uid) for uid in giveaway.get("awarded_user_ids", [])) if u}
    failed = {u for u in (_giveaway_to_int(uid) for uid in previous.get("failed_user_ids", [])) if u}

    # Security: Verify guild context matches giveaway ownership
    if str(guild.id) != str(giveaway.get("guild_id")):
        print(f"[Giveaway] Delivery aborted: context guild {guild.id} != giveaway guild {giveaway.get('guild_id')}")
        return

    delivered_now = 0
    failed_now = 0
    failed_now_ids: list[int] = []
    log_channel = await _resolve_giveaway_log_channel(guild, giveaway)

    for user_id in winner_ids:
        if user_id in awarded:
            failed.discard(user_id)
            continue

        key_value, key_error = await _request_giveaway_key_from_webhook(tier)
        if not key_value:
            failed_now += 1
            failed.add(user_id)
            failed_now_ids.append(user_id)
            print(f"[Giveaway] Key gen failed for user {user_id}: {key_error}")
            await _send_giveaway_key_log(log_channel, giveaway, winner_user_id=user_id, tier=tier,
                                         status_label="Generation Failed", detail=str(key_error or "Unknown"))
            continue

        member_obj = guild.get_member(user_id)
        if member_obj is None:
            try:
                member_obj = await guild.fetch_member(user_id)
            except Exception:
                member_obj = None

        if member_obj is None:
            failed_now += 1
            failed.add(user_id)
            failed_now_ids.append(user_id)
            await _send_giveaway_key_log(log_channel, giveaway, winner_user_id=user_id, tier=tier,
                                         status_label="User Lookup Failed", key_value=key_value,
                                         detail="Winner is not in guild anymore.")
            continue

        ticket_channel, ticket_error = await _create_giveaway_key_ticket(
            guild, giveaway, member_obj, key_value=key_value, tier=tier
        )
        if ticket_channel is None:
            failed_now += 1
            failed.add(user_id)
            failed_now_ids.append(user_id)
            await _send_giveaway_key_log(log_channel, giveaway, winner_user_id=user_id, tier=tier,
                                         status_label="Ticket Failed", key_value=key_value,
                                         detail=str(ticket_error or "Unknown ticket creation error"))
            continue

        delivered_now += 1
        awarded.add(user_id)
        failed.discard(user_id)
        await _send_giveaway_key_log(log_channel, giveaway, winner_user_id=user_id, tier=tier,
                                     status_label="Delivered (Ticket)", key_value=key_value,
                                     detail=f"Key delivered in {ticket_channel.mention}.")

    giveaway["awarded_user_ids"] = sorted(awarded)
    giveaway["key_delivery"] = {
        "tier": tier,
        "delivered_count": len(awarded),
        "failed_count": len(failed),
        "failed_user_ids": sorted(failed),
        "last_delivery_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"tier": tier, "delivered_now": delivered_now, "failed_now": failed_now, "failed_user_ids_now": failed_now_ids}


# ── Core CRUD ─────────────────────────────────────────────────────────────────

async def create_giveaway(
    guild: discord.Guild,
    *,
    channel_id: int,
    reward_title: str,
    reward_description: str,
    winner_count: int,
    duration_minutes: int,
    host_id: int | None,
    ping_role_id: int | None = None,
    key_log_channel_id: int | None = None,
    emoji: str = GIVEAWAY_JOIN_EMOJI,
    key_tier: str = "none",
) -> tuple[bool, str, dict | None]:
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            channel = None
    if channel is None or not hasattr(channel, "send"):
        return False, "Target channel could not be found.", None

    cleaned_title = reward_title.strip()[:256]
    if len(cleaned_title) < 2:
        return False, "Reward title must be at least 2 characters.", None

    now_utc = datetime.now(timezone.utc)
    minutes = _giveaway_clamp_duration_minutes(duration_minutes)
    end_at = now_utc + timedelta(minutes=minutes)
    join_emoji = str(emoji or GIVEAWAY_JOIN_EMOJI).strip() or GIVEAWAY_JOIN_EMOJI
    selected_tier = _normalize_giveaway_key_tier(key_tier)

    # Key rewards are now available to all configured servers.

    giveaway = {
        "guild_id": guild.id, "channel_id": channel_id, "message_id": None,
        "host_id": host_id or guild.owner_id,
        "reward_title": cleaned_title,
        "reward_description": reward_description.strip()[:3000],
        "winner_count": _giveaway_clamp_winner_count(winner_count),
        "duration_minutes": minutes,
        "ping_role_id": ping_role_id, "key_log_channel_id": key_log_channel_id,
        "emoji": join_emoji,
        "created_at": now_utc.isoformat(), "end_at": end_at.isoformat(),
        "ended": False, "ended_at": None, "ended_by": None, "ended_reason": "",
        "entry_count": 0, "winners": [], "jump_url": None,
        "reroll_count": 0, "last_rerolled_at": None, "last_rerolled_by": None,
        "last_reroll_winners": [], "last_reroll_winner_count": None,
        "entries_snapshot": [], "key_tier": selected_tier, "key_delivery": None, "awarded_user_ids": [],
    }

    mention = f"<@&{ping_role_id}>" if ping_role_id else None
    try:
        sent = await channel.send(
            content=mention, embed=_giveaway_build_embed(giveaway),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
    except Exception as e:
        return False, f"Failed to send giveaway message: {e}", None

    try:
        await sent.add_reaction(join_emoji)
    except Exception:
        try:
            await sent.add_reaction(GIVEAWAY_JOIN_EMOJI)
            giveaway["emoji"] = GIVEAWAY_JOIN_EMOJI
        except Exception:
            pass

    giveaway["message_id"] = sent.id
    giveaway["jump_url"] = sent.jump_url

    store = load_giveaways()
    store.setdefault(str(guild.id), {})[str(sent.id)] = giveaway
    save_giveaways(store)

    try:
        await sent.edit(embed=_giveaway_build_embed(giveaway))
    except Exception:
        pass

    return True, "Giveaway started.", giveaway


async def conclude_giveaway(
    guild: discord.Guild,
    message_id: int,
    *,
    ended_by_user_id: int | None,
    reason: str,
) -> tuple[bool, str, dict | None]:
    store = load_giveaways()
    guild_map = store.get(str(guild.id), {})
    giveaway = guild_map.get(str(message_id))
    if not isinstance(giveaway, dict):
        return False, "Giveaway not found.", None
    if giveaway.get("ended"):
        return False, "This giveaway is already ended.", giveaway

    # Security: Cross-verify guild context
    if str(guild.id) != str(giveaway.get("guild_id")):
        return False, "Security error: Guild context mismatch.", None

    entries = await _collect_giveaway_entries(guild, giveaway)
    winner_count = _giveaway_clamp_winner_count(giveaway.get("winner_count"))
    winners = _giveaway_pick_winners(entries, winner_count)

    now_utc = datetime.now(timezone.utc)
    giveaway.update({
        "ended": True, "ended_at": now_utc.isoformat(),
        "ended_by": ended_by_user_id, "ended_reason": str(reason or "manual"),
        "entry_count": len(entries), "winners": winners, "entries_snapshot": list(entries),
    })

    if winners and _normalize_giveaway_key_tier(giveaway.get("key_tier")) != "none":
        await _deliver_giveaway_keys(guild, giveaway, winners)

    guild_map[str(message_id)] = giveaway
    store[str(guild.id)] = guild_map
    save_giveaways(store)

    channel, message = await _fetch_giveaway_message(guild, giveaway)
    if message is not None:
        try:
            await message.edit(embed=_giveaway_build_embed(giveaway))
        except Exception:
            pass
        reaction_emoji = str(giveaway.get("emoji") or GIVEAWAY_JOIN_EMOJI).strip() or GIVEAWAY_JOIN_EMOJI
        try:
            await message.clear_reaction(reaction_emoji)
        except Exception:
            pass

    reward_title = str(giveaway.get("reward_title") or "this giveaway")
    if winners:
        winner_mentions = ", ".join(f"<@{u}>" for u in winners)
        announcement = (
            f"🎉 Giveaway ended for **{reward_title}**. "
            f"🏆 Winner(s) ({len(winners)}/{winner_count}): {winner_mentions}"
        )
        if len(winners) < winner_count:
            announcement += " | Not enough unique entrants to fill all winner slots."
    else:
        announcement = f"🎉 Giveaway ended for **{reward_title}**. No valid entries were found."

    if channel is not None and hasattr(channel, "send"):
        try:
            await channel.send(content=announcement, allowed_mentions=discord.AllowedMentions(users=True))
        except Exception:
            pass

    return True, announcement, giveaway


async def reroll_giveaway(
    guild: discord.Guild,
    message_id: int,
    *,
    rerolled_by_user_id: int | None,
    reroll_winner_count: int | None = None,
) -> tuple[bool, str, dict | None]:
    store = load_giveaways()
    guild_map = store.get(str(guild.id), {})
    giveaway = guild_map.get(str(message_id))
    if not isinstance(giveaway, dict):
        return False, "Giveaway not found.", None
    if not giveaway.get("ended"):
        return False, "End the giveaway before rerolling.", giveaway

    # Security: Cross-verify guild context
    if str(guild.id) != str(giveaway.get("guild_id")):
        return False, "Security error: Guild context mismatch.", None

    entries = await _collect_giveaway_entries(guild, giveaway)
    if not entries:
        return False, "No valid entries found for reroll.", giveaway

    winner_count = _giveaway_clamp_winner_count(giveaway.get("winner_count"))
    if reroll_winner_count is None:
        requested = winner_count
    else:
        parsed = _giveaway_to_int(reroll_winner_count)
        if not parsed or parsed < 1:
            return False, "Reroll amount must be at least 1.", giveaway
        if parsed > winner_count:
            return False, f"Reroll amount cannot exceed original winner count ({winner_count}).", giveaway
        requested = parsed

    excluded = {
        _giveaway_to_int(u)
        for u in (giveaway.get("winners") or []) + (giveaway.get("last_reroll_winners") or [])
    }
    excluded = {u for u in excluded if u}
    reroll_winners = _giveaway_pick_winners(entries, requested, excluded=excluded)
    if not reroll_winners:
        return False, "No eligible participants available for reroll.", giveaway

    if _normalize_giveaway_key_tier(giveaway.get("key_tier")) != "none":
        await _deliver_giveaway_keys(guild, giveaway, reroll_winners)

    giveaway.update({
        "entry_count": len(entries),
        "reroll_count": max(0, _giveaway_to_int(giveaway.get("reroll_count")) or 0) + 1,
        "last_rerolled_at": datetime.now(timezone.utc).isoformat(),
        "last_rerolled_by": rerolled_by_user_id,
        "last_reroll_winners": reroll_winners,
        "last_reroll_winner_count": requested,
    })

    guild_map[str(message_id)] = giveaway
    store[str(guild.id)] = guild_map
    save_giveaways(store)

    channel, message = await _fetch_giveaway_message(guild, giveaway)
    if message is not None:
        try:
            await message.edit(embed=_giveaway_build_embed(giveaway))
        except Exception:
            pass

    reward_title = str(giveaway.get("reward_title") or "this giveaway")
    winner_mentions = ", ".join(f"<@{u}>" for u in reroll_winners)
    announcement = (
        f"🔁 Reroll complete for **{reward_title}**. "
        f"🏆 New winner(s) ({len(reroll_winners)}/{requested}): {winner_mentions}"
    )
    if len(reroll_winners) < requested:
        announcement += " | Not enough unique entrants to fill all winner slots."

    if channel is not None and hasattr(channel, "send"):
        try:
            await channel.send(content=announcement, allowed_mentions=discord.AllowedMentions(users=True))
        except Exception:
            pass

    return True, announcement, giveaway


async def delete_giveaway(
    guild: discord.Guild,
    message_id: int,
    *,
    deleted_by_user_id: int | None,
) -> tuple[bool, str, dict | None]:
    store = load_giveaways()
    guild_map = store.get(str(guild.id), {})
    giveaway = guild_map.get(str(message_id))
    if not isinstance(giveaway, dict):
        return False, "Giveaway not found.", None
    if not giveaway.get("ended"):
        return False, "Only ended giveaways can be deleted.", giveaway

    _, message = await _fetch_giveaway_message(guild, giveaway)
    if message is not None:
        try:
            await message.delete()
        except Exception:
            pass

    guild_map.pop(str(message_id), None)
    if guild_map:
        store[str(guild.id)] = guild_map
    else:
        store.pop(str(guild.id), None)
    save_giveaways(store)
    return True, "Giveaway deleted.", giveaway


# ── Background loop ───────────────────────────────────────────────────────────

@tasks.loop(seconds=30)
async def giveaway_end_check():
    try:
        now_utc = datetime.now(timezone.utc)
        giveaways = await asyncio.to_thread(load_json, GIVEAWAYS_FILE, {})
        for guild_id, guild_entries in giveaways.items():
            gid = _giveaway_to_int(guild_id)
            if not gid or not isinstance(guild_entries, dict):
                continue
            guild = _bot.get_guild(gid)
            if guild is None:
                continue
            for message_id, entry in list(guild_entries.items()):
                if not isinstance(entry, dict) or entry.get("ended"):
                    continue
                end_at = _giveaway_parse_time(entry.get("end_at"))
                if end_at is None or end_at > now_utc:
                    continue
                ga_id = _giveaway_to_int(message_id)
                if not ga_id:
                    continue
                success, _, _ = await conclude_giveaway(guild, ga_id, ended_by_user_id=None, reason="automatic")
                if success:
                    print(f"[Giveaway] Auto-ended {ga_id} in guild {guild.id}.")
    except Exception as e:
        print(f"[Giveaway] Auto-end loop error: {e}")


@giveaway_end_check.before_loop
async def _before_giveaway_end_check():
    await _bot.wait_until_ready()


# ── Giveaway Key Claim Monitoring Loop ───────────────────────────────────────

@tasks.loop(seconds=30)
async def giveaway_claim_check():
    """Monitor unclaimed keys and send reminders or void expired keys."""
    try:
        now_utc = datetime.now(timezone.utc)
        giveaways = await asyncio.to_thread(load_json, GIVEAWAYS_FILE, {})
        
        modified = False
        
        for guild_id, guild_entries in giveaways.items():
            gid = _giveaway_to_int(guild_id)
            if not gid or not isinstance(guild_entries, dict):
                continue
            guild = _bot.get_guild(gid)
            if guild is None:
                continue
            
            for message_id, giveaway in list(guild_entries.items()):
                if not isinstance(giveaway, dict):
                    continue
            
            tracking = giveaway.get("key_claim_tracking", {})
            if not tracking:
                continue
            
            for channel_id_str, claim_data in list(tracking.items()):
                if claim_data.get("claim_status") != "pending":
                    continue
                
                claim_deadline = _giveaway_parse_time(claim_data.get("claim_deadline_at"))
                if claim_deadline is None:
                    continue
                
                channel_id = _giveaway_to_int(channel_id_str)
                if not channel_id:
                    continue
                
                # Check if deadline has passed (key should be voided)
                if now_utc >= claim_deadline:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        try:
                            # Send final warning message
                            await channel.send(
                                embed=discord.Embed(
                                    title="⏰ Time Expired",
                                    description=(
                                        "The 10-minute claim deadline has passed.\n"
                                        "This key has been voided and this channel will be closed in 10 seconds."
                                    ),
                                    color=discord.Color.red(),
                                )
                            )
                            await asyncio.sleep(10)
                            await channel.delete(reason="Giveaway key claim deadline expired")
                            print(f"[Giveaway] Voided unclaimed key and closed channel {channel_id}")
                        except Exception as e:
                            print(f"[Giveaway] Failed to close expired claim channel {channel_id}: {e}")
                    
                    # Update statusA
                    claim_data["claim_status"] = "voided"
                    claim_data["voided_at"] = now_utc.isoformat()
                    modified = True
                    
                    # Log to giveaway log channel
                    log_channel = await _resolve_giveaway_log_channel(guild, giveaway)
                    if log_channel:
                        user_id = claim_data.get("user_id")
                        tier = claim_data.get("tier", "unknown")
                        await _send_giveaway_key_log(
                            log_channel, giveaway,
                            winner_user_id=user_id,
                            tier=tier,
                            status_label="Voided (Unclaimed)",
                            detail=f"Winner did not claim key within 10 minutes. Channel closed."
                        )
                    continue
                
                # Check if 5-minute reminder should be sent
                time_remaining = claim_deadline - now_utc
                if time_remaining <= timedelta(minutes=5) and not claim_data.get("reminder_sent"):
                    channel = guild.get_channel(channel_id)
                    if channel:
                        user_id = claim_data.get("user_id")
                        try:
                            await channel.send(
                                content=f"<@{user_id}>",
                                embed=discord.Embed(
                                    title="⏰ Reminder: 5 Minutes Left!",
                                    description=(
                                        f"You have **5 minutes** remaining to claim your key!\n\n"
                                        f"Please click the **\"✅ I have saved my key\"** button above to confirm.\n\n"
                                        f"If you don't claim within 5 minutes, this key will be voided and the channel will close."
                                    ),
                                    color=discord.Color.orange(),
                                ),
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                            )
                            claim_data["reminder_sent"] = True
                            modified = True
                            print(f"[Giveaway] Sent 5-minute reminder for channel {channel_id}")
                        except Exception as e:
                            print(f"[Giveaway] Failed to send reminder for channel {channel_id}: {e}")
        
        if modified:
            await asyncio.to_thread(save_json, GIVEAWAYS_FILE, giveaways)
            
    except Exception as e:
        print(f"[Giveaway] Claim check loop error: {e}")


@giveaway_claim_check.before_loop
async def _before_giveaway_claim_check():
    await _bot.wait_until_ready()


# ── Slash commands ────────────────────────────────────────────────────────────

giveaway_group = app_commands.Group(name="giveaway", description="Giveaway commands")


@giveaway_group.command(name="create", description="Create a giveaway with reaction entry")
@app_commands.describe(
    channel="Channel where the giveaway will be posted",
    reward="Reward title",
    description="Reward description (optional)",
    winners="Number of winners",
    duration_minutes="Duration in minutes (1-10080)",
    ping_role="Optional role to ping when giveaway starts",
    key_log_channel="Optional channel to log generated giveaway keys",
    key_tier="Optional key tier for automatic winner delivery",
)
@app_commands.choices(key_tier=[
    app_commands.Choice(name="No key reward", value="none"),
    app_commands.Choice(name="Weekly key", value="weekly"),
    app_commands.Choice(name="Monthly key", value="monthly"),
    app_commands.Choice(name="Lifetime key", value="lifetime"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_create(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    reward: str,
    description: str = "",
    winners: app_commands.Range[int, 1, 25] = 1,
    duration_minutes: app_commands.Range[int, 1, 10080] = 60,
    ping_role: discord.Role = None,
    key_log_channel: discord.TextChannel = None,
    key_tier: app_commands.Choice[str] = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    success, message, giveaway = await create_giveaway(
        interaction.guild,
        channel_id=channel.id, reward_title=reward, reward_description=description,
        winner_count=winners, duration_minutes=duration_minutes,
        host_id=interaction.user.id,
        ping_role_id=ping_role.id if ping_role else None,
        key_log_channel_id=key_log_channel.id if key_log_channel else None,
        key_tier=key_tier.value if key_tier else "none",
    )
    if not success or not giveaway:
        await interaction.followup.send(f"❌ {message}", ephemeral=True)
        return
    jump_url = giveaway.get("jump_url")
    response = (
        f"✅ Giveaway created in {channel.mention}.\n"
        f"Giveaway ID: `{giveaway.get('message_id')}`\n"
        "Members can join by reacting with 🎉."
    )
    if jump_url:
        response += f"\n[Jump to giveaway]({jump_url})"
    await interaction.followup.send(response, ephemeral=True)


@giveaway_group.command(name="end", description="End an active giveaway now")
@app_commands.describe(message_id="Giveaway message ID")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_end(interaction: discord.Interaction, message_id: str):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Server only.", ephemeral=True)
        return
    gid = _giveaway_to_int(message_id)
    if not gid:
        await interaction.response.send_message("❌ Invalid giveaway message ID.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    success, message, _ = await conclude_giveaway(interaction.guild, gid, ended_by_user_id=interaction.user.id, reason="manual")
    if not success:
        await interaction.followup.send(f"❌ {message}", ephemeral=True)
    else:
        await interaction.followup.send(f"✅ Giveaway ended. {message}", ephemeral=True)


@giveaway_group.command(name="reroll", description="Pick new winner(s) from giveaway entrants")
@app_commands.describe(message_id="Giveaway message ID", winners="Optional reroll winner amount")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_reroll(interaction: discord.Interaction, message_id: str, winners: int | None = None):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Server only.", ephemeral=True)
        return
    gid = _giveaway_to_int(message_id)
    if not gid:
        await interaction.response.send_message("❌ Invalid giveaway message ID.", ephemeral=True)
        return
    if winners is not None and (winners < 1 or winners > 25):
        await interaction.response.send_message("❌ Reroll winner amount must be between 1 and 25.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    success, message, _ = await reroll_giveaway(interaction.guild, gid, rerolled_by_user_id=interaction.user.id, reroll_winner_count=winners)
    if not success:
        await interaction.followup.send(f"❌ {message}", ephemeral=True)
    else:
        await interaction.followup.send(f"✅ Reroll complete. {message}", ephemeral=True)


@giveaway_group.command(name="delete", description="Delete an ended giveaway")
@app_commands.describe(message_id="Giveaway message ID")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_delete(interaction: discord.Interaction, message_id: str):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Server only.", ephemeral=True)
        return
    gid = _giveaway_to_int(message_id)
    if not gid:
        await interaction.response.send_message("❌ Invalid giveaway message ID.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    success, message, _ = await delete_giveaway(interaction.guild, gid, deleted_by_user_id=interaction.user.id)
    if not success:
        await interaction.followup.send(f"❌ {message}", ephemeral=True)
    else:
        await interaction.followup.send("✅ Giveaway deleted.", ephemeral=True)


@giveaway_group.command(name="list", description="Show recent giveaways in this server")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_list(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Server only.", ephemeral=True)
        return
    guild_giveaways = load_giveaways().get(str(interaction.guild.id), {})
    if not guild_giveaways:
        await interaction.response.send_message("📭 No giveaways found for this server yet.", ephemeral=True)
        return

    now_utc = datetime.now(timezone.utc)
    entries = []
    for mid, giveaway in guild_giveaways.items():
        if not isinstance(giveaway, dict):
            continue
        end_at = _giveaway_parse_time(giveaway.get("end_at"))
        created_at = _giveaway_parse_time(giveaway.get("created_at"))
        entries.append((mid, giveaway, end_at, created_at))

    entries.sort(key=lambda x: x[3] or x[2] or datetime.fromtimestamp(0, tz=timezone.utc), reverse=True)
    lines = []
    for mid, giveaway, end_at, _ in entries[:15]:
        title = str(giveaway.get("reward_title") or "Mystery Reward")
        ended = bool(giveaway.get("ended"))
        status = "ENDED" if ended else ("ENDING" if end_at and end_at <= now_utc else "ACTIVE")
        end_label = f"<t:{int(end_at.timestamp())}:R>" if end_at else "Unknown"
        lines.append(f"`{mid}` • {status} • {title[:60]} • {end_label}")

    embed = discord.Embed(
        title=f"🎉 Giveaways — {interaction.guild.name}",
        description="\n".join(lines),
        color=discord.Color.blurple(),
        timestamp=now_utc,
    )
    embed.set_footer(text="Use /giveaway end, /giveaway reroll [winners], or /giveaway delete with a message ID")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: discord.ext.commands.Bot):
    global _bot
    _bot = bot
    bot.tree.add_command(giveaway_group)
    
    # Add persistent view for claim buttons
    bot.add_view(GiveawayKeyClaimView(giveaway_id=None, channel_id=0, user_id=0))
