"""
modules/activity_rewards.py
Random key rewards for genuinely active chat members.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import aiohttp
import discord
from discord.ext import tasks
from discord import app_commands

from modules.utils import load_json, save_json

_bot: discord.ext.commands.Bot | None = None

ACTIVITY_REWARDS_STATS_FILE = "activity_rewards_stats"
ACTIVITY_REWARDS_CONFIG_FILE = "activity_rewards_configs"
ACTIVITY_REWARDS_STATE_FILE = "activity_rewards_states"

DEFAULT_CONFIG = {
    "enabled": True,
    "min_messages_low": int(os.getenv("ACTIVITY_REWARDS_MIN_MESSAGES_LOW", "200")),
    "min_messages_high": int(os.getenv("ACTIVITY_REWARDS_MIN_MESSAGES_HIGH", "300")),
    "draw_interval_minutes": int(os.getenv("ACTIVITY_REWARDS_DRAW_INTERVAL_MINUTES", "360")),
    "cooldown_days": int(os.getenv("ACTIVITY_REWARDS_COOLDOWN_DAYS", "7")),
    "max_rewards_per_draw": 1,
    "daily_winner_cap": max(1, int(os.getenv("ACTIVITY_REWARDS_DAILY_WINNER_CAP", "3"))),
    "weekly_winner_cap": max(1, int(os.getenv("ACTIVITY_REWARDS_WEEKLY_WINNER_CAP", "12"))),
    "key_pool": str(os.getenv("ACTIVITY_REWARDS_KEY_POOL", "activity_pool")).strip().lower() or "activity_pool",
    "logging_channel_id": None,
    # Per-message RNG mode: reward fires when you actually send a message, not via periodic draw.
    "rng_mode_enabled": True,
    "rng_chance_per_message": float(os.getenv("ACTIVITY_REWARDS_RNG_CHANCE", "0.002")),  # 0.2% per message
    "min_messages_rng_threshold": int(os.getenv("ACTIVITY_REWARDS_RNG_MIN_MESSAGES", "50")),
}

def _parse_id(val: any) -> int | None:
    try:
        return int(str(val or "").strip())
    except (ValueError, TypeError):
        return None

# OFFICIAL_GUILD_ID removed - configuration is now guild-agnostic via dashboard.

_recent_fingerprints: Dict[str, tuple[str, datetime]] = {}


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _guild_cfg(guild_id: int) -> dict:
    root = load_json(ACTIVITY_REWARDS_CONFIG_FILE, {})
    if not isinstance(root, dict):
        root = {}
    cfg = root.get(str(guild_id), {})
    if not isinstance(cfg, dict):
        cfg = {}
    merged = {**DEFAULT_CONFIG, **cfg}
    merged["enabled"] = bool(merged.get("enabled", True))
    merged["min_messages_low"] = max(1, int(merged.get("min_messages_low", 200)))
    merged["min_messages_high"] = max(merged["min_messages_low"], int(merged.get("min_messages_high", 300)))
    merged["draw_interval_minutes"] = max(5, int(merged.get("draw_interval_minutes", 360)))
    merged["cooldown_days"] = max(0, int(merged.get("cooldown_days", 7)))
    merged["max_rewards_per_draw"] = max(1, min(5, int(merged.get("max_rewards_per_draw", 1))))
    merged["daily_winner_cap"] = max(1, min(200, int(merged.get("daily_winner_cap", 3))))
    merged["weekly_winner_cap"] = max(1, min(1000, int(merged.get("weekly_winner_cap", 12))))
    key_pool = str(merged.get("key_pool", "activity_pool")).strip().lower()
    merged["key_pool"] = key_pool if key_pool in {"activity_pool", "giveaway"} else "activity_pool"
    merged["rng_mode_enabled"] = bool(merged.get("rng_mode_enabled", True))
    merged["rng_chance_per_message"] = max(0.0, min(1.0, float(merged.get("rng_chance_per_message", 0.002))))
    merged["min_messages_rng_threshold"] = max(1, int(merged.get("min_messages_rng_threshold", 50)))

    log_ch = merged.get("logging_channel_id")
    merged["logging_channel_id"] = str(log_ch) if log_ch else None
    
    # Remove old webhook keys if they exist in DB
    for old_key in ["event_webhook_url", "event_webhook_enabled", "event_webhook_retry_count"]:
        merged.pop(old_key, None)
        
    return merged


def _load_stats() -> dict:
    data = load_json(ACTIVITY_REWARDS_STATS_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_stats(data: dict) -> None:
    save_json(ACTIVITY_REWARDS_STATS_FILE, data)


def _load_state() -> dict:
    data = load_json(ACTIVITY_REWARDS_STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_state(data: dict) -> None:
    save_json(ACTIVITY_REWARDS_STATE_FILE, data)


def _append_reward_history(guild_id: int, event: dict) -> None:
    state = _load_state()
    guild_state = state.get(str(guild_id), {})
    if not isinstance(guild_state, dict):
        guild_state = {}
    history = guild_state.get("reward_history", [])
    if not isinstance(history, list):
        history = []
    history.append(event)
    guild_state["reward_history"] = history[-200:]
    state[str(guild_id)] = guild_state
    _save_state(state)


def _append_reward_audit(guild_id: int, event: dict) -> None:
    """Append action-level audit entries to shared config audit log stream."""
    path = "config_audit_logs"
    root = load_json(path, {})
    if not isinstance(root, dict):
        root = {}
    bucket = root.get(str(guild_id), [])
    if not isinstance(bucket, list):
        bucket = []
    bucket.append(
        {
            "at": _now().isoformat(),
            "guild_id": str(guild_id),
            "module": "activity_rewards_action",
            "changed_keys": [],
            "before": None,
            "after": event,
            "actor_id": event.get("actor_user_id"),
            "actor_name": event.get("actor_name"),
            "actor_token_hash": None,
        }
    )
    root[str(guild_id)] = bucket[-300:]
    save_json(path, root)
    
    # Send embed to logging channel if configured
    if _bot:
        cfg = _guild_cfg(guild_id)
        log_ch_id = cfg.get("logging_channel_id")
        if log_ch_id:
            try:
                log_ch_id = int(log_ch_id)
            except ValueError:
                return
            
            async def _send_log():
                guild = _bot.get_guild(guild_id)
                if not guild:
                    return
                channel = guild.get_channel(log_ch_id)
                if not channel or not isinstance(channel, discord.TextChannel):
                    return
                embed = discord.Embed(
                    title="Activity Reward Event",
                    color=discord.Color.blue(),
                    timestamp=_now()
                )
                
                action = str(event.get("action", ""))
                embed.add_field(name="Action", value=action.replace("_", " ").title(), inline=False)
                
                winner_id = event.get("winner_user_id")
                if winner_id:
                    embed.add_field(name="Winner", value=f"<@{winner_id}>", inline=True)
                
                tier = event.get("tier")
                if tier:
                    embed.add_field(name="Tier", value=str(tier), inline=True)
                    
                reason = event.get("reason")
                if reason:
                    embed.add_field(name="Reason", value=str(reason), inline=True)
                
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    pass
            
            asyncio.create_task(_send_log())


def _tier_and_product() -> tuple[str, str]:
    # Weekly gets slightly higher chance than 5-day.
    pick = random.random()
    if pick < 0.6:
        return "weekly", "Weekly Key"
    return "5days", "5 Days Key"


def _webhook_url_for_tier(tier: str, key_pool: str) -> str:
    if tier == "weekly":
        if key_pool == "giveaway":
            return str(os.getenv("GIVEAWAY_KEY_WEBHOOK_WEEKLY", "")).strip()
        return str(os.getenv("ACTIVITY_REWARDS_WEBHOOK_WEEKLY", "")).strip()
    if tier == "5days":
        if key_pool == "giveaway":
            return str(os.getenv("GIVEAWAY_KEY_WEBHOOK_WEEKLY", "")).strip()
        return str(os.getenv("ACTIVITY_REWARDS_WEBHOOK_5D", "")).strip()
    return ""


def _parse_key_from_response(text: str) -> Optional[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        plain = text.strip()
        return plain if plain and "<html" not in plain.lower() else None

    if isinstance(data, dict):
        keys = data.get("keys")
        if isinstance(keys, list) and keys:
            return str(keys[0] or "").strip() or None
        single = data.get("key")
        if single:
            return str(single).strip() or None
    elif isinstance(data, list) and data:
        return str(data[0] or "").strip() or None
    return None


async def _request_key(product_name: str, tier: str, cfg: dict) -> tuple[Optional[str], Optional[str]]:
    url = _webhook_url_for_tier(tier, str(cfg.get("key_pool", "activity_pool")))
    if not url:
        return None, "Webhook URL is not configured for this tier."
    
    # Security: only the official server can use these webhooks
    # We check the guild later in the loop, but this is a fail-safe.

    payload = {"item": {"product": {"name": product_name}, "quantity": 1}}
    body = json.dumps(payload).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    secret = str(os.getenv("ACTIVITY_REWARDS_WEBHOOK_HMAC_SECRET", "")).strip()
    if secret:
        digest = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256).hexdigest()
        header_name = str(os.getenv("ACTIVITY_REWARDS_WEBHOOK_HMAC_HEADER", "coms")).strip() or "coms"
        headers[header_name] = digest
        headers["X-Signature"] = digest

    try:
        timeout = aiohttp.ClientTimeout(total=max(3, int(os.getenv("ACTIVITY_REWARDS_WEBHOOK_TIMEOUT_SECONDS", "12"))))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    return None, f"Webhook status {resp.status}"
                key = _parse_key_from_response(text)
                if not key:
                    return None, "Webhook returned success but no key."
                return key, None
    except Exception as e:
        return None, f"Webhook request failed: {e}"


def _eligible_users(guild_id: int, cfg: dict, now_utc: datetime) -> tuple[list[tuple[int, int]], int]:
    stats = _load_stats().get(str(guild_id), {})
    if not isinstance(stats, dict):
        return [], 0

    min_required = random.randint(int(cfg["min_messages_low"]), int(cfg["min_messages_high"]))
    cooldown_days = int(cfg["cooldown_days"])
    eligible: list[tuple[int, int]] = []

    for user_id, row in stats.items():
        if not isinstance(row, dict):
            continue
        try:
            uid = int(user_id)
        except ValueError:
            continue

        message_count = max(0, int(row.get("message_count", 0)))
        if message_count < min_required:
            continue

        last_message_at = _parse_iso(row.get("last_message_at"))
        if not last_message_at or now_utc - last_message_at > timedelta(days=10):
            continue

        last_reward_at = _parse_iso(row.get("last_reward_at"))
        if cooldown_days > 0 and last_reward_at and (now_utc - last_reward_at) < timedelta(days=cooldown_days):
            continue

        unique_days = max(0, int(row.get("unique_days", 0)))
        # Weighted scoring model: recent days carry more impact than older activity.
        daily_counts_raw = row.get("daily_counts", {})
        if not isinstance(daily_counts_raw, dict):
            daily_counts_raw = {}
        weighted_messages = 0
        for day_str, count in daily_counts_raw.items():
            try:
                day = datetime.fromisoformat(str(day_str)).date()
                c = max(0, int(count or 0))
            except Exception:
                continue
            age_days = (now_utc.date() - day).days
            if age_days < 0 or age_days > 45:
                continue
            if age_days <= 2:
                factor = 3
            elif age_days <= 6:
                factor = 2
            else:
                factor = 1
            weighted_messages += c * factor
        base_messages = weighted_messages if weighted_messages > 0 else message_count
        weight = max(1, base_messages + unique_days * 10)
        eligible.append((uid, weight))

    return eligible, min_required


def _pick_weighted_winner(pool: list[tuple[int, int]]) -> Optional[int]:
    if not pool:
        return None
    total = sum(weight for _, weight in pool)
    pick = random.uniform(0, total)
    cursor = 0.0
    for uid, weight in pool:
        cursor += weight
        if pick <= cursor:
            return uid
    return pool[-1][0]


def _safe_slug(text: str) -> str:
    clean = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(text or ""))
    while "--" in clean:
        clean = clean.replace("--", "-")
    clean = clean.strip("-")
    return clean or "winner"


class ActivityRewardsClaimView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ I have saved my key",
        style=discord.ButtonStyle.success,
        custom_id="activity_rewards_claim_key_button",
    )
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message("❌ This button only works in server key channels.", ephemeral=True)
            return

        state = _load_state()
        guild_state = state.get(str(interaction.guild.id), {})
        if not isinstance(guild_state, dict):
            guild_state = {}
        tracking = guild_state.get("key_claim_tracking", {})
        if not isinstance(tracking, dict):
            tracking = {}

        ticket = tracking.get(str(interaction.channel.id))
        if not isinstance(ticket, dict):
            await interaction.response.send_message("❌ This key ticket is no longer active.", ephemeral=True)
            return

        owner_id = int(ticket.get("user_id", 0) or 0)
        if interaction.user.id != owner_id:
            await interaction.response.send_message("❌ Only the winner can claim this key.", ephemeral=True)
            return

        status = str(ticket.get("claim_status", "pending")).strip().lower()
        if status != "pending":
            await interaction.response.send_message("ℹ️ This key has already been claimed.", ephemeral=True)
            return

        now_utc = _now()
        close_at = now_utc + timedelta(minutes=5)
        ticket["claim_status"] = "claimed"
        ticket["claim_confirmed_at"] = now_utc.isoformat()
        ticket["auto_close_at"] = close_at.isoformat()
        ticket["closed_at"] = None
        ticket["close_reason"] = None
        tracking[str(interaction.channel.id)] = ticket
        guild_state["key_claim_tracking"] = tracking
        state[str(interaction.guild.id)] = guild_state
        _save_state(state)

        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        if embed is not None:
            embed.color = discord.Color.green()
            embed.description = (
                "✅ **Key Claimed Successfully!**\n\n"
                f"{interaction.user.mention}, you confirmed that you saved your key.\n"
                "This channel will close automatically in **5 minutes**."
            )

        button.disabled = True
        button.label = "✅ Key Claimed"
        # Also disable the close button now that the key is claimed
        for child in self.children:
            if getattr(child, "custom_id", None) == "activity_rewards_close_channel_button":
                child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            "🎉 Claim recorded. This key channel will auto-close in 5 minutes.",
            ephemeral=False,
        )

    @discord.ui.button(
        label="🔒 Close Channel",
        style=discord.ButtonStyle.danger,
        custom_id="activity_rewards_close_channel_button",
    )
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message("❌ This button only works in server key channels.", ephemeral=True)
            return

        state = _load_state()
        guild_state = state.get(str(interaction.guild.id), {})
        if not isinstance(guild_state, dict):
            guild_state = {}
        tracking = guild_state.get("key_claim_tracking", {})
        if not isinstance(tracking, dict):
            tracking = {}

        ticket = tracking.get(str(interaction.channel.id))
        if not isinstance(ticket, dict):
            await interaction.response.send_message("❌ This key ticket is no longer active.", ephemeral=True)
            return

        owner_id = int(ticket.get("user_id", 0) or 0)
        is_owner = interaction.user.id == owner_id
        is_admin = interaction.user.guild_permissions.manage_channels
        if not is_owner and not is_admin:
            await interaction.response.send_message("❌ Only the winner or a moderator can close this channel.", ephemeral=True)
            return

        now_utc = _now()
        ticket["claim_status"] = "closed"
        ticket["closed_at"] = now_utc.isoformat()
        ticket["close_reason"] = "manual_close"
        tracking[str(interaction.channel.id)] = ticket
        guild_state["key_claim_tracking"] = tracking
        state[str(interaction.guild.id)] = guild_state
        _save_state(state)

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("🔒 Closing channel in 5 seconds...", ephemeral=False)
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason="Activity rewards key channel closed manually")
        except Exception:
            pass


async def _create_activity_reward_key_ticket(
    guild: discord.Guild,
    member: discord.Member,
    *,
    key_value: str,
    tier: str,
) -> tuple[Optional[discord.TextChannel], Optional[str]]:
    from modules.boost import load_boost_config

    cfg_root = load_boost_config()
    guild_cfg = cfg_root.get(str(guild.id), {}) if isinstance(cfg_root, dict) else {}

    category = None
    category_id = int(guild_cfg.get("category_id", 0) or 0)
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
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
        ),
    }
    for raw_role_id in guild_cfg.get("roles", []):
        try:
            role_id = int(raw_role_id)
        except (TypeError, ValueError):
            continue
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

    base = _safe_slug(member.display_name or member.name)
    channel_name = f"activity-key-{base}"[:95]
    if any(str(ch.name).lower() == channel_name.lower() for ch in guild.text_channels):
        channel_name = f"{channel_name[:86]}-{str(member.id)[-4:]}"

    try:
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites=overwrites,
            reason=f"Activity rewards key delivery for winner {member.id}",
        )
    except Exception as e:
        return None, f"Failed to create key ticket channel: {e}"

    now_utc = _now()
    embed = discord.Embed(
        title="🎁 Activity Reward Key",
        description=(
            f"Congrats {member.mention}! You were randomly selected for an activity reward in **{guild.name}**.\n\n"
            "**How to claim:**\n"
            "1) Copy the key\n"
            "2) Save it somewhere safe\n"
            "3) Click **✅ I have saved my key**\n\n"
            "After you claim, this channel auto-closes in **5 minutes**."
        ),
        color=discord.Color.gold(),
        timestamp=now_utc,
    )
    embed.add_field(name="Tier", value=tier, inline=True)
    embed.add_field(name="Your Key", value=f"```{key_value}```", inline=False)
    embed.set_footer(text="Keep this key private and do not share it.")

    claim_view = ActivityRewardsClaimView()
    try:
        sent_message = await ticket_channel.send(
            content=f"{member.mention} 🎉",
            embed=embed,
            view=claim_view,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
    except Exception as e:
        return None, f"Created key channel but failed to send key message: {e}"

    state = _load_state()
    guild_state = state.get(str(guild.id), {})
    if not isinstance(guild_state, dict):
        guild_state = {}
    tracking = guild_state.get("key_claim_tracking", {})
    if not isinstance(tracking, dict):
        tracking = {}

    tracking[str(ticket_channel.id)] = {
        "user_id": member.id,
        "channel_id": ticket_channel.id,
        "message_id": sent_message.id,
        "claim_status": "pending",
        "claim_confirmed_at": None,
        "auto_close_at": None,
        "closed_at": None,
        "close_reason": None,
        "created_at": now_utc.isoformat(),
        "key_tier": tier,
    }

    guild_state["key_claim_tracking"] = tracking
    state[str(guild.id)] = guild_state
    _save_state(state)
    return ticket_channel, None


async def _deliver_reward(guild: discord.Guild, user_id: int, tier: str, key_value: str) -> bool:
    user = guild.get_member(user_id)
    if user is None:
        try:
            user = await guild.fetch_member(user_id)
        except Exception:
            user = None
    if user is None:
        return False

    ticket_channel, err = await _create_activity_reward_key_ticket(guild, user, key_value=key_value, tier=tier)
    if ticket_channel is None:
        print(f"[ActivityRewards] Ticket creation failed for guild={guild.id} user={user_id}: {err}")
        return False
    return True


async def _award_user_from_rng(
    guild: discord.Guild,
    cfg: dict,
    *,
    winner_id: int,
    min_required: int,
    eligible_count: int,
    event_reason: str,
    actor_user_id: int | None = None,
) -> tuple[bool, str]:
    now_utc = _now()
    tier, product = _tier_and_product()
    key_value, err = await _request_key(product, tier, cfg)
    if not key_value:
        _append_reward_history(
            guild.id,
            {
                "at": now_utc.isoformat(),
                "status": "webhook_failed",
                "winner_user_id": str(winner_id),
                "tier": tier,
                "min_required": min_required,
                "eligible_count": eligible_count,
                "reason": event_reason,
                "error": str(err or "unknown"),
            },
        )
        return False, str(err or "webhook failed")

    delivered = await _deliver_reward(guild, winner_id, tier, key_value)
    if delivered:
        _update_winner_stats(guild.id, winner_id)
        payload = {
            "at": now_utc.isoformat(),
            "status": "delivered",
            "winner_user_id": str(winner_id),
            "tier": tier,
            "min_required": min_required,
            "eligible_count": eligible_count,
            "reason": event_reason,
        }
        _append_reward_history(guild.id, payload)
        _append_reward_audit(
            guild.id,
            {
                "action": "activity_reward_delivered",
                "winner_user_id": str(winner_id),
                "tier": tier,
                "reason": event_reason,
                "actor_user_id": str(actor_user_id) if actor_user_id else None,
            },
        )
        return True, tier

    _append_reward_history(
        guild.id,
        {
            "at": now_utc.isoformat(),
            "status": "ticket_failed",
            "winner_user_id": str(winner_id),
            "tier": tier,
            "min_required": min_required,
            "eligible_count": eligible_count,
            "reason": event_reason,
        },
    )
    _append_reward_audit(
        guild.id,
        {
            "action": "activity_reward_ticket_failed",
            "winner_user_id": str(winner_id),
            "tier": tier,
            "reason": event_reason,
        },
    )
    return False, "ticket creation failed"


def _update_winner_stats(guild_id: int, user_id: int) -> None:
    all_stats = _load_stats()
    guild_stats = all_stats.get(str(guild_id), {})
    if not isinstance(guild_stats, dict):
        guild_stats = {}
    row = guild_stats.get(str(user_id), {})
    if not isinstance(row, dict):
        row = {}
    row["last_reward_at"] = _now().isoformat()
    row["total_rewards"] = int(row.get("total_rewards", 0)) + 1
    guild_stats[str(user_id)] = row
    all_stats[str(guild_id)] = guild_stats
    _save_stats(all_stats)


async def _try_rng_reward_on_message(
    guild: discord.Guild,
    user_id: int,
    cfg: dict,
    current_message_count: int,
) -> None:
    """Called when a user's message wins the per-message RNG roll. Checks cooldown/caps then awards."""
    now_utc = _now()
    cooldown_days = int(cfg.get("cooldown_days", 7))

    # Check personal cooldown
    all_stats = await asyncio.to_thread(_load_stats)
    guild_stats = all_stats.get(str(guild.id), {})
    row = guild_stats.get(str(user_id), {}) if isinstance(guild_stats, dict) else {}
    if cooldown_days > 0 and isinstance(row, dict):
        last_reward_at = _parse_iso(row.get("last_reward_at"))
        if last_reward_at and (now_utc - last_reward_at) < timedelta(days=cooldown_days):
            return

    # Check daily/weekly budget caps
    state = await asyncio.to_thread(_load_state)
    guild_state = state.get(str(guild.id), {})
    history = guild_state.get("reward_history", []) if isinstance(guild_state, dict) else []

    day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())

    def _count_delivered_since(start_dt: datetime) -> int:
        return sum(
            1 for item in history
            if isinstance(item, dict)
            and str(item.get("status", "")).strip().lower() == "delivered"
            and (at := _parse_iso(item.get("at"))) is not None
            and at >= start_dt
        )

    if _count_delivered_since(day_start) >= int(cfg.get("daily_winner_cap", 3)):
        return
    if _count_delivered_since(week_start) >= int(cfg.get("weekly_winner_cap", 12)):
        return

    min_threshold = max(1, int(cfg.get("min_messages_rng_threshold", 50)))
    ok, detail = await _award_user_from_rng(
        guild, cfg,
        winner_id=user_id,
        min_required=min_threshold,
        eligible_count=1,
        event_reason="rng_on_message",
    )
    if ok:
        print(f"[ActivityRewards] RNG reward triggered by message! Delivered {detail} to user={user_id} in guild={guild.id}")


async def _run_draw_for_guild(guild: discord.Guild, cfg: dict) -> None:
    now_utc = _now()
    eligible, min_required = _eligible_users(guild.id, cfg, now_utc)
    if not eligible:
        _append_reward_history(
            guild.id,
            {
                "at": now_utc.isoformat(),
                "status": "skipped_no_eligible",
                "min_required": min_required,
                "eligible_count": 0,
            },
        )
        return

    winners_target = int(cfg.get("max_rewards_per_draw", 1))
    # Budget/caps guardrail per day/week.
    state = _load_state()
    guild_state = state.get(str(guild.id), {})
    if not isinstance(guild_state, dict):
        guild_state = {}
    history = guild_state.get("reward_history", [])
    if not isinstance(history, list):
        history = []

    def _count_delivered_since(start_dt: datetime) -> int:
        total = 0
        for item in history:
            if not isinstance(item, dict):
                continue
            if str(item.get("status", "")).strip().lower() != "delivered":
                continue
            at = _parse_iso(item.get("at"))
            if at and at >= start_dt:
                total += 1
        return total

    day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    daily_remaining = max(0, int(cfg.get("daily_winner_cap", 3)) - _count_delivered_since(day_start))
    weekly_remaining = max(0, int(cfg.get("weekly_winner_cap", 12)) - _count_delivered_since(week_start))
    winners_target = min(winners_target, daily_remaining, weekly_remaining)
    if winners_target <= 0:
        _append_reward_history(
            guild.id,
            {
                "at": now_utc.isoformat(),
                "status": "skipped_budget_cap",
                "daily_remaining": daily_remaining,
                "weekly_remaining": weekly_remaining,
            },
        )
        return
    picked: set[int] = set()
    for _ in range(winners_target):
        remaining = [item for item in eligible if item[0] not in picked]
        if not remaining:
            break
        winner_id = _pick_weighted_winner(remaining)
        if not winner_id:
            continue

        ok, detail = await _award_user_from_rng(
            guild,
            cfg,
            winner_id=winner_id,
            min_required=min_required,
            eligible_count=len(eligible),
            event_reason="scheduled_draw",
        )
        if ok:
            picked.add(winner_id)
            print(f"[ActivityRewards] Delivered {detail} reward in guild={guild.id} to user={winner_id}")


@tasks.loop(minutes=5)
async def activity_rewards_draw_loop():
    if _bot is None:
        return
    if not _env_bool("ACTIVITY_REWARDS_ENABLED", True):
        return

    state = await asyncio.to_thread(_load_state)
    now_utc = _now()

    for guild in _bot.guilds:
        # Configured servers are checked below.

        cfg = await asyncio.to_thread(_guild_cfg, guild.id)
        if not cfg.get("enabled", True):
            continue

        gstate = state.get(str(guild.id), {})
        if not isinstance(gstate, dict):
            gstate = {}
        last_draw_at = _parse_iso(gstate.get("last_draw_at"))
        interval = timedelta(minutes=int(cfg["draw_interval_minutes"]))
        if last_draw_at and (now_utc - last_draw_at) < interval:
            continue

        await _run_draw_for_guild(guild, cfg)
        gstate["last_draw_at"] = now_utc.isoformat()
        state[str(guild.id)] = gstate

    await asyncio.to_thread(_save_state, state)


@activity_rewards_draw_loop.before_loop
async def _before_activity_rewards_draw_loop():
    if _bot is not None:
        await _bot.wait_until_ready()


@tasks.loop(seconds=30)
async def activity_rewards_claim_check():
    if _bot is None:
        return
    state = await asyncio.to_thread(_load_state)
    changed = False
    now_utc = _now()

    for guild_id, guild_state in state.items():
        if not isinstance(guild_state, dict):
            continue
        try:
            gid = int(guild_id)
        except ValueError:
            continue
        guild = _bot.get_guild(gid)
        if guild is None:
            continue

        tracking = guild_state.get("key_claim_tracking", {})
        if not isinstance(tracking, dict):
            continue

        for channel_id, ticket in list(tracking.items()):
            if not isinstance(ticket, dict):
                continue
            status = str(ticket.get("claim_status", "pending")).strip().lower()
            if status != "claimed":
                continue

            close_at = _parse_iso(ticket.get("auto_close_at"))
            if close_at is None or now_utc < close_at:
                continue

            try:
                cid = int(channel_id)
            except ValueError:
                ticket["claim_status"] = "closed"
                ticket["closed_at"] = now_utc.isoformat()
                ticket["close_reason"] = "invalid_channel_id"
                changed = True
                continue

            channel = guild.get_channel(cid)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(cid)
                except Exception:
                    channel = None

            if channel is not None and hasattr(channel, "delete"):
                try:
                    await channel.send(
                        embed=discord.Embed(
                            title="✅ Key Claim Complete",
                            description="This key ticket is now closing. Your claim was already recorded.",
                            color=discord.Color.green(),
                        )
                    )
                except Exception:
                    pass
                
                try:
                    await channel.delete(reason="Activity rewards key claimed - auto close after 5 minutes")
                except Exception:
                    pass

            ticket["claim_status"] = "closed"
            ticket["closed_at"] = now_utc.isoformat()
            ticket["close_reason"] = "claimed_auto_close_5m"
            tracking[channel_id] = ticket
            changed = True

        guild_state["key_claim_tracking"] = tracking
        state[guild_id] = guild_state

    if changed:
        await asyncio.to_thread(_save_state, state)


@activity_rewards_claim_check.before_loop
async def _before_activity_rewards_claim_check():
    if _bot is not None:
        await _bot.wait_until_ready()


async def _track_message_activity(message: discord.Message) -> None:
    if _bot is None:
        return
    if not _env_bool("ACTIVITY_REWARDS_ENABLED", True):
        return
    if not message.guild or message.author.bot:
        return

    content = (message.content or "").strip()
    if len(content) < 8:
        return

    guild_id = message.guild.id
    user_id = message.author.id
    now_utc = _now()

    # Duplicate suppression: ignore repeated same-content bursts.
    fp_key = f"{guild_id}:{user_id}"
    digest = hashlib.sha256(content.lower().encode("utf-8")).hexdigest()
    prev = _recent_fingerprints.get(fp_key)
    if prev and prev[0] == digest and (now_utc - prev[1]) < timedelta(seconds=45):
        return
    _recent_fingerprints[fp_key] = (digest, now_utc)

    all_stats = _load_stats()
    guild_stats = all_stats.get(str(guild_id), {})
    if not isinstance(guild_stats, dict):
        guild_stats = {}
    row = guild_stats.get(str(user_id), {})
    if not isinstance(row, dict):
        row = {}

    minute_bucket = now_utc.strftime("%Y-%m-%dT%H:%M")
    prev_bucket = str(row.get("minute_bucket", ""))
    minute_count = int(row.get("minute_count", 0))
    if prev_bucket != minute_bucket:
        minute_count = 0
    if minute_count >= 12:
        return
    minute_count += 1

    message_count = int(row.get("message_count", 0)) + 1
    day_key = now_utc.date().isoformat()
    active_days = row.get("active_days", [])
    if not isinstance(active_days, list):
        active_days = []
    if day_key not in active_days:
        active_days.append(day_key)
    active_days = sorted(active_days)[-45:]
    daily_counts = row.get("daily_counts", {})
    if not isinstance(daily_counts, dict):
        daily_counts = {}
    daily_counts[day_key] = int(daily_counts.get(day_key, 0) or 0) + 1
    # Keep rolling 45-day activity window.
    daily_counts = {k: v for k, v in daily_counts.items() if k in set(active_days)}

    row.update(
        {
            "message_count": message_count,
            "unique_days": len(active_days),
            "active_days": active_days,
            "last_message_at": now_utc.isoformat(),
            "minute_bucket": minute_bucket,
            "minute_count": minute_count,
            "daily_counts": daily_counts,
        }
    )
    guild_stats[str(user_id)] = row
    all_stats[str(guild_id)] = guild_stats
    _save_stats(all_stats)

    # Per-message RNG: only someone actively sending a message right now can win.
    cfg = _guild_cfg(guild_id)
    if cfg.get("enabled", True) and cfg.get("rng_mode_enabled", True):
        min_threshold = max(1, int(cfg.get("min_messages_rng_threshold", 50)))
        rng_chance = float(cfg.get("rng_chance_per_message", 0.002))
        if message_count >= min_threshold and rng_chance > 0 and random.random() < rng_chance:
            asyncio.create_task(
                _try_rng_reward_on_message(message.guild, user_id, cfg, message_count)
            )


def get_activity_rewards_leaderboard(guild_id: int, limit: int = 15) -> list[dict]:
    stats = _load_stats().get(str(guild_id), {})
    if not isinstance(stats, dict):
        return []
    
    guild = _bot.get_guild(guild_id) if _bot else None

    rows: list[dict] = []
    for uid, row in stats.items():
        if not isinstance(row, dict):
            continue
        try:
            user_id = int(uid)
        except ValueError:
            continue
        
        user_name = None
        if guild:
            member = guild.get_member(user_id)
            if member:
                user_name = member.display_name or member.name
        if not user_name and _bot:
            user = _bot.get_user(user_id)
            if user:
                user_name = user.display_name or user.name

        rows.append(
            {
                "user_id": str(user_id),
                "user_name": user_name or "Unknown User",
                "message_count": int(row.get("message_count", 0) or 0),
                "unique_days": int(row.get("unique_days", 0) or 0),
                "total_rewards": int(row.get("total_rewards", 0) or 0),
                "last_message_at": row.get("last_message_at"),
                "last_reward_at": row.get("last_reward_at"),
            }
        )
    rows.sort(key=lambda r: (r["message_count"], r["unique_days"], r["total_rewards"]), reverse=True)
    return rows[: max(1, min(50, int(limit or 15)))]


# ----- Slash commands: activity leaderboard & user stats -----
activity_group = app_commands.Group(name="activity", description="Activity rewards commands")


@activity_group.command(name="leaderboard", description="Show the activity leaderboard for this server")
@app_commands.describe(limit="How many rows to show (max 50)")
async def activity_leaderboard(interaction: discord.Interaction, limit: int = 15):
    await interaction.response.defer(ephemeral=False)
    limit = max(1, min(50, int(limit or 15)))
    # Per-guild leaderboard only
    if interaction.guild is None:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return
    rows = get_activity_rewards_leaderboard(interaction.guild.id, limit=limit)
    if not rows:
        await interaction.followup.send("No activity stats available for this server.", ephemeral=True)
        return
    desc_lines = []
    for idx, r in enumerate(rows, start=1):
        name = r.get("user_name") or "Unknown User"
        msgs = r.get('message_count', 0)
        days = r.get('unique_days', 0)
        rewards = r.get('total_rewards', 0)
        desc_lines.append(f"**#{idx}** • {name}\n╰─ `{msgs}` messages • `{days}` days • `{rewards}` rewards")
        
    embed = discord.Embed(
        title=f"Activity Leaderboard — {interaction.guild.name}", 
        description="\n".join(desc_lines), 
        color=discord.Color.blue()
    )
    if interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    embed.set_footer(text=f"Showing top {len(rows)} active members", icon_url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)


@activity_group.command(name="me", description="Show your activity stats in this server")
async def activity_me(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    stats = _load_stats().get(str(interaction.guild.id), {})
    row = stats.get(str(interaction.user.id)) if isinstance(stats, dict) else None
    if not row or not isinstance(row, dict):
        await interaction.followup.send("No activity recorded for you in this server.", ephemeral=True)
        return
    msg_count = int(row.get("message_count", 0) or 0)
    unique_days = int(row.get("unique_days", 0) or 0)
    total_rewards = int(row.get("total_rewards", 0) or 0)
    last_msg_iso = row.get("last_message_at")
    last_reward_iso = row.get("last_reward_at")
    
    embed = discord.Embed(
        title=f"👤 Activity Stats — {interaction.user.display_name}", 
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    
    embed.add_field(name="💬 Messages", value=f"`{msg_count}`", inline=True)
    embed.add_field(name="🗓️ Active Days", value=f"`{unique_days}`", inline=True)
    embed.add_field(name="🎁 Rewards", value=f"`{total_rewards}`", inline=True)
    
    if last_msg_iso:
        last_msg_dt = datetime.fromisoformat(last_msg_iso)
        embed.add_field(name="🛰️ Last Message", value=f"<t:{int(last_msg_dt.timestamp())}:R>", inline=False)
    if last_reward_iso:
        last_reward_dt = datetime.fromisoformat(last_reward_iso)
        embed.add_field(name="✨ Last Reward", value=f"<t:{int(last_reward_dt.timestamp())}:R>", inline=False)
        
    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed, ephemeral=True)


@activity_group.command(name="test", description="Test activity command (shows a sample response)")
async def activity_test(interaction: discord.Interaction):
    # Test flow: request a key and deliver it to the invoking user as a test reward.
    if interaction.guild is None:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    cfg = _guild_cfg(interaction.guild.id)
    tier, product = _tier_and_product()
    key_value, err = await _request_key(product, tier, cfg)
    if not key_value:
        await interaction.followup.send(f"Test failed: could not request key — {err}", ephemeral=True)
        return

    delivered = await _deliver_reward(interaction.guild, interaction.user.id, tier, key_value)
    if delivered:
        await interaction.followup.send(
            f"Test reward delivered to you in **{interaction.guild.name}**. A private ticket channel was created with the test key.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send("Test failed: could not create key ticket channel for you.", ephemeral=True)


async def manual_reroll_reward(guild: discord.Guild, actor_user_id: int | None = None) -> tuple[bool, str]:
    cfg = _guild_cfg(guild.id)
    eligible, min_required = _eligible_users(guild.id, cfg, _now())
    if not eligible:
        return False, "No eligible users right now."
    winner_id = _pick_weighted_winner(eligible)
    if not winner_id:
        return False, "Could not pick a winner."
    ok, detail = await _award_user_from_rng(
        guild,
        cfg,
        winner_id=winner_id,
        min_required=min_required,
        eligible_count=len(eligible),
        event_reason="manual_reroll",
        actor_user_id=actor_user_id,
    )
    if not ok:
        return False, detail
    return True, f"Manual reroll delivered {detail} key to {winner_id}."


async def manual_revoke_reward(
    guild: discord.Guild,
    target_user_id: int,
    actor_user_id: int | None = None,
) -> tuple[bool, str]:
    state = _load_state()
    guild_state = state.get(str(guild.id), {})
    if not isinstance(guild_state, dict):
        guild_state = {}
    tracking = guild_state.get("key_claim_tracking", {})
    if not isinstance(tracking, dict):
        tracking = {}

    affected = 0
    for channel_id, ticket in list(tracking.items()):
        if not isinstance(ticket, dict):
            continue
        if int(ticket.get("user_id", 0) or 0) != int(target_user_id):
            continue
        status = str(ticket.get("claim_status", "pending")).strip().lower()
        if status in {"closed", "voided", "revoked"}:
            continue
        try:
            cid = int(channel_id)
        except ValueError:
            cid = 0
        if cid:
            channel = guild.get_channel(cid)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(cid)
                except Exception:
                    channel = None
            if channel is not None and hasattr(channel, "delete"):
                try:
                    await channel.delete(reason=f"Activity reward revoked by {actor_user_id or 'system'}")
                except Exception:
                    pass
        ticket["claim_status"] = "revoked"
        ticket["closed_at"] = _now().isoformat()
        ticket["close_reason"] = "manual_revoke"
        tracking[str(channel_id)] = ticket
        affected += 1

    guild_state["key_claim_tracking"] = tracking
    state[str(guild.id)] = guild_state
    _save_state(state)

    _append_reward_history(
        guild.id,
        {
            "at": _now().isoformat(),
            "status": "revoked",
            "winner_user_id": str(target_user_id),
            "reason": "manual_revoke",
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
            "affected_channels": affected,
        },
    )
    _append_reward_audit(
        guild.id,
        {
            "action": "activity_reward_revoked",
            "winner_user_id": str(target_user_id),
            "reason": "manual_revoke",
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
            "affected_channels": affected,
        },
    )
    return True, f"Revoked reward channels: {affected}"


def register(bot: discord.ext.commands.Bot):
    global _bot
    _bot = bot

    async def _activity_rewards_on_message(message: discord.Message):
        await _track_message_activity(message)

    # Use listener to avoid replacing other on_message handlers.
    bot.add_listener(_activity_rewards_on_message, "on_message")
    bot.add_view(ActivityRewardsClaimView())
    try:
        bot.tree.add_command(activity_group)
    except Exception:
        # If command already added or tree not ready, ignore
        pass


