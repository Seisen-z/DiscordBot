"""
modules/boost.py
Server boost system – config, ClaimKeyView, event handler, slash commands.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from urllib.parse import quote
from datetime import timedelta

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from modules.utils import load_json, save_json, BOOST_CONFIG_FILE

# In-memory store: { guild_id: { user_id: "KEY_HERE" } }
_unclaimed_keys: dict[int, dict[int, str]] = {}

# Serialize delivery, unboost and retry for a guild across network awaits.
_reward_locks: dict[int, asyncio.Lock] = {}


def _reward_lock(guild_id: int) -> asyncio.Lock:
    return _reward_locks.setdefault(guild_id, asyncio.Lock())


BOOST_REWARD_ROLE_ID = 1402982327935832104

def _parse_id(val: any) -> int | None:
    try:
        return int(str(val or "").strip())
    except (ValueError, TypeError):
        return None

# OFFICIAL_GUILD_ID removed - configuration is now guild-agnostic via dashboard.


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_boost_config() -> dict:
    return load_json(BOOST_CONFIG_FILE, {})


def save_boost_config(data: dict):
    save_json(BOOST_CONFIG_FILE, data)


# ── View ──────────────────────────────────────────────────────────────────────

class CloseTicketView(discord.ui.View):
    """
    Persistent view (timeout=None + static custom_id) registered once via
    bot.add_view() in register(). Ownership can't be stored on the instance
    because the same registered instance handles clicks for every boost
    channel ever created, including ones from before the last restart — it
    must be looked up per-channel from the boost config instead.
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="boost:close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Everything below is wrapped so a bad config read or any other
        # unexpected error still gets a response back within Discord's 3s
        # window — otherwise the client shows a bare "didn't respond in
        # time" with no indication of what actually went wrong.
        try:
            cfg = load_boost_config()
            guild_id = str(interaction.guild_id)
            gcfg = cfg.get(guild_id, {})
            open_tickets = gcfg.get("open_boost_tickets", {})
            owner_id = open_tickets.get(str(interaction.channel_id))
            is_owner = owner_id is not None and int(owner_id) == interaction.user.id
            is_staff = interaction.user.guild_permissions.manage_channels

            if not (is_owner or is_staff):
                await interaction.response.send_message("❌ Only the booster or staff can close this ticket!", ephemeral=True)
                return

            await interaction.response.send_message("🔒 Closing ticket in 3 seconds...")

            if str(interaction.channel_id) in open_tickets:
                del open_tickets[str(interaction.channel_id)]
                gcfg["open_boost_tickets"] = open_tickets
                cfg[guild_id] = gcfg
                save_boost_config(cfg)

            await asyncio.sleep(3)
            await interaction.channel.delete(reason="Boost ticket closed.")
        except discord.HTTPException as e:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Failed to delete channel: {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Failed to close ticket: {e}", ephemeral=True)
        except Exception as e:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Unexpected error while closing: {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Unexpected error while closing: {e}", ephemeral=True)


# ── Webhook key helper ────────────────────────────────────────────────────────

async def _request_boost_key(webhook_url: str, webhook_secret: str | None) -> str | None:
    payload = {"item": {"product": {"name": "Premium Key"}, "quantity": 1}}
    payload_bytes = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if webhook_secret:
        sig = hmac.new(webhook_secret.encode(), msg=payload_bytes, digestmod=hashlib.sha256).hexdigest()
        for k in ["X-Signature", "x-jnkie-signature", "signature", "jnkie-signature",
                  "Authorization", "X-Hub-Signature", "X-Hub-Signature-256",
                  "Discord-Boosting", "X-Discord-Boosting", "Discord-Boosting-Signature", "DiscordBoosting"]:
            headers[k] = sig
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload, headers=headers) as resp:
                text = await resp.text()
                if resp.status == 200:
                    try:
                        data = json.loads(text)
                        if "keys" in data and data["keys"]:
                            return data["keys"][0]
                        if "key" in data:
                            return data["key"]
                    except json.JSONDecodeError:
                        if not text.strip().startswith("<") and text.strip():
                            return text.strip()
                else:
                    print(f"[Boost] Webhook failed with status {resp.status}: {text}")
    except Exception as e:
        print(f"[Boost] Exception generating key via webhook: {e}")
    return None


async def _request_boost_key_with_retry(webhook_url: str, webhook_secret: str | None, attempts: int = 3) -> str | None:
    """A single transient webhook failure used to mean the booster got no key at all."""
    for attempt in range(attempts):
        key = await _request_boost_key(webhook_url, webhook_secret)
        if key:
            return key
        if attempt < attempts - 1:
            await asyncio.sleep(2 * (attempt + 1))
    return None


# ── Unboost penalty ───────────────────────────────────────────────────────────

DEFAULT_PENALTY = {"mode": "warn", "days": 30}


def _record_key_claim(guild_id: int, user_id: int, key: str, boost_event=None, boost_started_at=None, days_per_boost: int = 15):
    """Persist the exact reward before delivery; keep history after unboosting."""
    cfg = load_boost_config()
    gcfg = cfg[str(guild_id)]
    gcfg.setdefault("key_claims", {})[str(user_id)] = {
        "claimed_at": discord.utils.utcnow().isoformat(),
        "key": key,
        "revocation_status": "active",
        "credited_units": 0,
        "delivery_status": "pending",
        "boost_started_at": boost_started_at.isoformat() if boost_started_at else None,
        "boost_events": {},
    }
    event_id, units = boost_event or ("initial", 1)
    claim = gcfg["key_claims"][str(user_id)]
    claim["initial_event_id"] = event_id
    claim["boost_events"][event_id] = {
        "units": units, "extra_units": units, "kind": "initial",
        "days_per_boost": days_per_boost, "status": "pending",
    }
    if not boost_event:
        claim["notification_cutoff"] = claim["claimed_at"]
    save_boost_config(cfg)


DEFAULT_DAYS_PER_BOOST = 15


def _days_per_boost(gcfg: dict) -> int:
    days = gcfg.get("days_per_boost", DEFAULT_DAYS_PER_BOOST)
    if type(days) is not int or not 1 <= days <= 365:
        raise ValueError("Days per boost must be an integer from 1 to 365")
    return days


def _initial_duration_target(record: dict, days: int) -> dict:
    if record.get("used_at"):
        raise ValueError("Generated key is already used")
    target = {"validity_minutes": days * 1440}
    if record.get("expires_at") or record.get("expiry_on_first_use") is not True:
        target["expires_at"] = (discord.utils.utcnow() + timedelta(days=days)).isoformat()
    return target


_BOOST_MESSAGE_TYPES = {
    discord.MessageType.premium_guild_subscription, discord.MessageType.premium_guild_tier_1,
    discord.MessageType.premium_guild_tier_2, discord.MessageType.premium_guild_tier_3,
}


async def _reward_key_request(method: str, key: str, payload=None):
    token = os.getenv("JUNKIE_API_KEY", "").strip()
    if not token:
        return None, "missing_api_key"
    url = "https://api.jnkie.com/api/v2/keys/" + quote(key, safe="")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.request(method, url, json=payload,
                    headers={"Authorization": f"Bearer {token}"}, allow_redirects=False) as resp:
                if resp.status != 200:
                    return None, f"{method.lower()}_http_{resp.status}"
                if method == "PUT":
                    return {}, None
                data = await resp.json()
                record = data.get("key") if isinstance(data, dict) else None
                if not isinstance(record, dict) or record.get("key_value") != key:
                    return None, "key_readback_mismatch"
                return record, None
    except Exception:
        return None, "request_failed"  # Never log token, key URL or response bodies.


def _extension_target(record: dict, units: int, days_per_boost: int = 15) -> dict:
    delta = timedelta(days=days_per_boost * units)
    expires = discord.utils.parse_time(record.get("expires_at"))
    if expires:
        return {"expires_at": (max(expires, discord.utils.utcnow()) + delta).isoformat()}
    minutes = record.get("validity_minutes")
    if record.get("expiry_on_first_use") is True and type(minutes) is int and minutes > 0:
        return {"validity_minutes": minutes + days_per_boost * 1440 * units}
    raise ValueError("Key duration requires manual review")  # Do not shorten lifetime keys.


def _target_satisfied(record: dict, target: dict, exact: bool = False) -> bool:
    if record.get("is_invalidated") is not False:
        return False
    if "expires_at" in target:
        expires = discord.utils.parse_time(record.get("expires_at"))
        desired = discord.utils.parse_time(target["expires_at"])
        matches = expires is not None and (abs((expires - desired).total_seconds()) <= 1 if exact
                                           else expires >= desired - timedelta(seconds=1))
        if not matches:
            return False
        return (not exact or "validity_minutes" not in target
                or record.get("validity_minutes") == target["validity_minutes"])
    minutes = record.get("validity_minutes")
    return (not record.get("expires_at") and type(minutes) is int
            and (minutes == target["validity_minutes"] if exact else minutes >= target["validity_minutes"]))


async def _apply_boost_credits(guild_id: int, user_id: str):
    """Caller holds the guild lock. Persist absolute targets before PUT for retries."""
    cfg = load_boost_config()
    claim = cfg.get(str(guild_id), {}).get("key_claims", {}).get(user_id)
    if not isinstance(claim, dict) or claim.get("unboosted_at") or not claim.get("key"):
        return
    for event_id in claim.get("boost_events", {}):
        cfg = load_boost_config()
        claim = cfg[str(guild_id)]["key_claims"][user_id]
        event = claim["boost_events"][event_id]
        if event["status"] == "applied":
            continue
        if event["status"] != "pending":
            break
        key = claim["key"]
        record, error = await _reward_key_request("GET", key)
        target = event.get("target")
        initial = event.get("kind") == "initial"
        days = event.get("days_per_boost", DEFAULT_DAYS_PER_BOOST)
        if not error and record.get("is_invalidated") is not False:
            error = "invalidated_key_manual_review"
        if not error:
            try:
                target = target or (_initial_duration_target(record, days * event["extra_units"]) if initial
                                    else _extension_target(record, event["extra_units"], days))
                # A first-use key may be redeemed between preparation and retry.
                # Convert the saved total validity into an absolute expiry, not another duration increment.
                if "validity_minutes" in target and "expires_at" not in target and record.get("expires_at"):
                    used = discord.utils.parse_time(record.get("used_at"))
                    if used is None:
                        raise ValueError("Missing first-use anchor")
                    target = {"expires_at": (used + timedelta(minutes=target["validity_minutes"])).isoformat()}
            except (ValueError, TypeError, OverflowError):
                error = "duration_manual_review"
        if not error:
            cfg = load_boost_config()
            event = cfg[str(guild_id)]["key_claims"][user_id]["boost_events"][event_id]
            event["target"] = target
            save_boost_config(cfg)  # Same target is reused after timeout or restart.
            if not _target_satisfied(record, target, exact=initial):
                _, error = await _reward_key_request("PUT", key, target)
                if not error:
                    record, error = await _reward_key_request("GET", key)
                if not error and not _target_satisfied(record, target, exact=initial):
                    error = "extension_readback_unconfirmed"
        cfg = load_boost_config()
        claim = cfg[str(guild_id)]["key_claims"][user_id]
        event = claim["boost_events"][event_id]
        event["error"] = error
        if error:
            if error.endswith("manual_review"):
                event["status"] = "manual_review"
            save_boost_config(cfg)
            break  # Preserve ordering: later events must build on confirmed duration.
        event["status"] = "applied"
        claim["credited_units"] = claim.get("credited_units", 1) + event["extra_units"]
        save_boost_config(cfg)


async def _retry_boost_credits(bot: discord.Client):
    for guild_id in load_boost_config():
        async with _reward_lock(int(guild_id)):
            claims = load_boost_config().get(guild_id, {}).get("key_claims", {})
            for user_id, claim in claims.items():
                if isinstance(claim, dict) and (claim.get("delivery_status") == "pending" or any(
                        e.get("status") == "pending" for e in claim.get("boost_events", {}).values())):
                    guild = bot.get_guild(int(guild_id))
                    if guild is None:
                        continue
                    member = guild.get_member(int(user_id))
                    if member is None:
                        try:
                            member = await guild.fetch_member(int(user_id))
                        except discord.HTTPException:
                            continue
                    if member.premium_since is None:
                        await _apply_unboost_penalty(guild, member)
                        continue
                    started = claim.get("boost_started_at")
                    if started and discord.utils.parse_time(started) != member.premium_since:
                        continue  # A different boost session needs staff reconciliation.
                    await _apply_boost_credits(int(guild_id), user_id)
                    await _deliver_recorded_reward(guild, member)
                    updated = load_boost_config()[guild_id]["key_claims"][user_id]
                    if updated.get("credited_units", 1) > claim.get("credited_units", 1):
                        await _notify_penalty(guild, member,
                            f"Boost credit retry confirmed for {member.mention}: {updated['credited_units']} credited boosts.")


async def handle_boost_message(message: discord.Message):
    """Only Discord system boost messages credit time; normal text never does."""
    if (message.guild is None or message.type not in _BOOST_MESSAGE_TYPES
            or message.author.bot or message.webhook_id is not None):
        return "Not a Discord boost notification."
    content = message.content.strip()
    if content and (not content.isdecimal() or int(content) < 1):
        return "Boost quantity needs manual review."
    units = int(content) if content else 1
    guild = message.guild
    async with _reward_lock(guild.id):
        member = guild.get_member(message.author.id)
        if member is None or member.premium_since is None:
            try:
                member = await guild.fetch_member(message.author.id)
            except discord.HTTPException:
                return "Member lookup failed; replay the notification later."
        if member.premium_since is None:
            return "Member is not currently boosting."
        if message.created_at < member.premium_since:
            return "Notification predates the current boost session."
        gcfg = load_boost_config().get(str(guild.id))
        if gcfg is None:
            return "Configure boost logging first."
        user_id, event_id = str(member.id), str(message.id)
        claims = gcfg.get("key_claims", {})
        if user_id not in claims:
            key = await _deliver_boost_reward(guild, member, boost_event=(event_id, units))
            if not key:
                claim = load_boost_config().get(str(guild.id), {}).get("key_claims", {}).get(user_id)
                return ("Reward saved; duration or delivery is pending. Automatic retry is scheduled."
                        if isinstance(claim, dict) else "Key generation failed; replay later.")
            event = load_boost_config()[str(guild.id)]["key_claims"][user_id]["boost_events"][event_id]
            return f"Reward key generated; boost credit {event['status']}. Check the delivery log."
        claim = claims[user_id]
        if not isinstance(claim, dict) or not claim.get("key"):
            return "Legacy/revoked claim needs staff review; no replacement key issued."
        if claim.get("unboosted_at") or claim.get("revocation_status") != "active":
            return "Reward is revoked or pending revocation; no time credited."
        events = claim.setdefault("boost_events", {})
        if event_id in events and events[event_id]["status"] == "applied":
            return "Notification already credited; no additional time added."
        if event_id not in events:
            # Manual grants/old versions credited a first boost without an event ID.
            # Keep this cutoff after migration too, so replay cannot count it twice.
            cutoff = claim.get("notification_cutoff")
            if not events:
                cutoff = cutoff or claim.get("claimed_at")
            if cutoff and message.created_at <= discord.utils.parse_time(cutoff):
                return "Older notification needs staff reconciliation; no time credited."
            started = claim.get("boost_started_at")
            if ((started and discord.utils.parse_time(started) != member.premium_since)
                    or (not started and member.premium_since > discord.utils.parse_time(claim["claimed_at"]))):
                return "Boost session changed; staff review required."
            if cutoff:
                claim["notification_cutoff"] = cutoff
            if not started:
                claim["boost_started_at"] = member.premium_since.isoformat()
            events[event_id] = {"units": units, "extra_units": units, "status": "pending",
                                "days_per_boost": _days_per_boost(gcfg)}
            cfg = load_boost_config()
            cfg[str(guild.id)]["key_claims"][user_id] = claim
            save_boost_config(cfg)
        await _apply_boost_credits(guild.id, user_id)
        await _deliver_recorded_reward(guild, member)
        event = load_boost_config()[str(guild.id)]["key_claims"][user_id]["boost_events"][event_id]
        outcome = f"Boost credit {event_id}: {event['status']} (+{event['extra_units'] * event.get('days_per_boost', DEFAULT_DAYS_PER_BOOST)} days)."
        await _notify_penalty(guild, member, outcome)
        return outcome


async def _invalidate_boost_key(key: str) -> tuple[bool, str]:
    """Invalidate only this reward, and require provider readback confirmation."""
    token = os.getenv("JUNKIE_API_KEY", "").strip()
    if not token:
        return False, "missing_api_key"
    url = "https://api.jnkie.com/api/v2/keys/" + quote(key, safe="")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.put(url, json={"is_invalidated": True}, headers=headers,
                                   allow_redirects=False) as resp:
                if resp.status != 200:
                    return False, f"update_http_{resp.status}"
            async with session.get(url, headers=headers, allow_redirects=False) as resp:
                if resp.status != 200:
                    return False, f"readback_http_{resp.status}"
                data = await resp.json()
                record = data.get("key") if isinstance(data, dict) else None
                if isinstance(record, dict) and record.get("is_invalidated") is True:
                    return True, "confirmed"
                return False, "readback_not_invalidated"
    except Exception:
        # Exceptions and response bodies may contain the key URL or credentials.
        return False, "request_failed"


async def _revoke_claim(guild_id: int, user_id: str) -> bool:
    cfg = load_boost_config()
    claim = cfg.get(str(guild_id), {}).get("key_claims", {}).get(user_id)
    if not isinstance(claim, dict) or claim.get("revocation_status") != "pending":
        return False
    key = claim.get("key")
    if not key:
        return False
    success, error = await _invalidate_boost_key(key)
    # Re-read after network I/O to preserve intervening configuration writes.
    cfg = load_boost_config()
    claim = cfg[str(guild_id)]["key_claims"][user_id]
    claim["revocation_error"] = None if success else error
    if success:
        claim["revocation_status"] = "revoked"
        claim["revoked_at"] = discord.utils.utcnow().isoformat()
        claim.pop("key", None)
    save_boost_config(cfg)
    return success


async def _retry_revocations(bot: discord.Client):
    for guild_id in load_boost_config():
        async with _reward_lock(int(guild_id)):
            claims = load_boost_config().get(guild_id, {}).get("key_claims", {})
            for user_id, claim in claims.items():
                if isinstance(claim, dict) and claim.get("revocation_status") == "pending":
                    if await _revoke_claim(int(guild_id), user_id):
                        guild = bot.get_guild(int(guild_id))
                        if guild:
                            await _notify_penalty(guild, discord.Object(id=int(user_id)),
                                f"Boost reward key revoked for <@{user_id}> (provider confirmed).")


async def _notify_penalty(guild: discord.Guild, member: discord.abc.User, text: str):
    cfg = load_boost_config().get(str(guild.id), {})
    channel_id = _parse_id(cfg.get("channel_id"))
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel:
        try:
            await channel.send(text)
        except Exception as e:
            print(f"[Boost] Failed to send penalty log: {e}")


async def apply_unboost_penalty(guild: discord.Guild, member: discord.abc.User):
    async with _reward_lock(guild.id):
        await _apply_unboost_penalty(guild, member)


async def _apply_unboost_penalty(guild: discord.Guild, member: discord.abc.User):
    """Warn or temp-ban someone who received a boost key and then stopped boosting."""
    cfg = load_boost_config()
    gcfg = cfg.get(str(guild.id))
    if not gcfg:
        return
    claims = gcfg.get("key_claims", {})
    if str(member.id) not in claims:
        return  # never got a key from us (or boosted before tracking existed)

    penalty = {**DEFAULT_PENALTY, **gcfg.get("penalty", {})}
    claim = claims[str(member.id)]
    if not isinstance(claim, dict):
        claim = {"claimed_at": claim}  # Legacy timestamp: exact key was not stored.
        claims[str(member.id)] = claim
    if claim.get("unboosted_at"):
        return  # History remains, but duplicate member events do not repeat penalties.
    received_reward = claim.get("delivery_status") not in {"pending", "cancelled_unboost"}
    claim["unboosted_at"] = discord.utils.utcnow().isoformat()
    if claim.get("delivery_status") == "pending":
        claim["delivery_status"] = "cancelled_unboost"
    for event in claim.get("boost_events", {}).values():
        if event.get("status") == "pending":
            event["status"] = "cancelled_unboost"
    claim["revocation_status"] = "pending" if claim.get("key") else "manual_review"
    save_boost_config(cfg)  # Durable pending work survives restart or provider failure.
    _unclaimed_keys.get(guild.id, {}).pop(member.id, None)
    if claim["revocation_status"] == "pending":
        revoked = await _revoke_claim(guild.id, str(member.id))
        outcome = "revoked (provider confirmed)" if revoked else "pending; automatic retry scheduled"
    else:
        outcome = "needs manual review; legacy claim has no stored key"
    await _notify_penalty(guild, member, f"Boost reward for {member.mention}: {outcome}.")
    # Revocation wrote fresh state; do not overwrite it with the pre-request copy.
    cfg = load_boost_config()
    gcfg = cfg[str(guild.id)]
    if not received_reward:
        return  # Revoke an undelivered key without punishing a member who never got it.

    if penalty["mode"] == "ban":
        days = int(penalty["days"])
        expires = discord.utils.utcnow().timestamp() + days * 86400
        gcfg.setdefault("temp_bans", {})[str(member.id)] = expires
        save_boost_config(cfg)
        try:
            await member.send(
                f"You removed your boost from **{guild.name}** after receiving a boost key. "
                f"Your boost reward is no longer eligible; you have been banned for {days} days."
            )
        except Exception:
            pass
        try:
            await guild.ban(member, reason=f"Removed server boost after claiming key ({days}-day ban)", delete_message_days=0)
            await _notify_penalty(guild, member, f"🔨 {member.mention} (`{member.id}`) removed their boost after receiving a key and was banned for {days} days.")
        except Exception as e:
            print(f"[Boost] Failed to ban {member.id}: {e}")
            await _notify_penalty(guild, member, f"⚠️ {member.mention} removed their boost after receiving a key, but the ban failed: {e}")
    else:
        save_boost_config(cfg)
        try:
            await member.send(
                f"⚠️ You removed your boost from **{guild.name}** after receiving a boost key. "
                "Your boost reward is no longer eligible. Please contact staff with any questions."
            )
        except Exception:
            pass
        await _notify_penalty(guild, member, f"⚠️ {member.mention} (`{member.id}`) removed their boost after receiving a key and was warned.")


async def _expire_temp_bans(bot: discord.Client):
    now = discord.utils.utcnow().timestamp()
    cfg = load_boost_config()
    changed = False
    for guild_id, gcfg in cfg.items():
        bans = gcfg.get("temp_bans", {})
        due = [uid for uid, exp in bans.items() if exp <= now]
        if not due:
            continue
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            continue
        for uid in due:
            try:
                await guild.unban(discord.Object(id=int(uid)), reason="Boost penalty ban expired")
            except discord.NotFound:
                pass  # already unbanned manually
            except Exception as e:
                print(f"[Boost] Failed to unban {uid}: {e}")
                continue
            del bans[uid]
            changed = True
    if changed:
        save_boost_config(cfg)


# ── on_member_update handler ─────────────────────────────────────────────────

async def deliver_boost_reward(guild: discord.Guild, member: discord.Member, is_test: bool = False):
    """Deliver one key; distinct verified boost messages extend that same key."""
    async with _reward_lock(guild.id):
        return await _deliver_boost_reward(guild, member, is_test)


async def _deliver_boost_reward(guild: discord.Guild, member: discord.Member, is_test: bool = False, boost_event=None):
    gcfg = load_boost_config().get(str(guild.id))
    if gcfg is None:
        return None
    boost_role = guild.get_role(BOOST_REWARD_ROLE_ID)
    if boost_role and boost_role not in member.roles:
        try:
            await member.add_roles(boost_role, reason="Started boosting the server (or test)")
        except Exception as e:
            print(f"[Boost] Failed to add boost role to {member.display_name}: {e}")

    if not is_test and str(member.id) in gcfg.get("key_claims", {}):
        return None

    days = _days_per_boost(gcfg)
    webhook_url = os.getenv("WEBHOOK_URL")
    webhook_secret = os.getenv("WEBHOOK_HMAC_SECRET")

    generated_key = None
    # Key delivery is now available to all configured servers.
    if webhook_url:
        generated_key = await _request_boost_key_with_retry(webhook_url, webhook_secret)

    if not generated_key:
        print(f"[Boost] No key generated for member {member.id}; webhook missing or failed.")
        return None
    if is_test:
        return await _send_boost_reward(guild, member, generated_key, is_test=True)
    _record_key_claim(guild.id, member.id, generated_key, boost_event,
                      getattr(member, "premium_since", None), days)
    await _apply_boost_credits(guild.id, str(member.id))
    delivered = await _deliver_recorded_reward(guild, member)
    if not delivered:
        await _notify_penalty(guild, member,
            f"Boost reward for {member.mention} is pending duration confirmation or delivery. Check provider configuration; saved work retries automatically.")
    return delivered


async def _deliver_recorded_reward(guild: discord.Guild, member: discord.Member):
    cfg = load_boost_config()
    claim = cfg.get(str(guild.id), {}).get("key_claims", {}).get(str(member.id))
    if not isinstance(claim, dict) or claim.get("delivery_status") != "pending" or claim.get("unboosted_at"):
        return None
    initial_id = claim["initial_event_id"]
    initial = claim["boost_events"][initial_id]
    if initial["status"] != "applied":
        return None  # Hold the webhook key until its configured duration is confirmed.
    key = claim["key"]
    boost_event = None if initial_id == "initial" else (initial_id, initial["units"])
    delivered = await _send_boost_reward(guild, member, key, boost_event=boost_event)
    cfg = load_boost_config()
    if delivered:
        cfg[str(guild.id)]["key_claims"][str(member.id)]["delivery_status"] = "sent"
        _unclaimed_keys.setdefault(guild.id, {})[member.id] = key
        save_boost_config(cfg)
    return delivered


async def _send_boost_reward(guild: discord.Guild, member: discord.Member, generated_key: str,
                             is_test: bool = False, boost_event=None):
    delivered = False
    async def _dm_fallback(reason: str):
        nonlocal delivered
        # Without this a missing config/channel silently swallowed the key.
        if not generated_key:
            return
        try:
            await member.send(f"Thanks for boosting **{guild.name}**! Your Premium Key:\n```{generated_key}```")
            delivered = True
        except Exception as e:
            print(f"[Boost] {reason}; could not DM key to {member.id}: {e}")

    cfg = load_boost_config()
    guild_id_str = str(guild.id)
    # Strict check: only proceed if the guild has explicitly saved a boost configuration.
    if guild_id_str not in cfg:
        print(f"[Boost] Skipping {guild.name} ({guild.id}) - no configuration found.")
        await _dm_fallback("no configuration")
        return generated_key if delivered else None

    guild_cfg = cfg.get(guild_id_str, {})
    channel_id = int(guild_cfg["channel_id"]) if guild_cfg.get("channel_id") else None

    if not channel_id:
        print(f"[Boost] Skipping {guild.name} ({guild.id}) - no log channel configured.")
        await _dm_fallback("no log channel")
        return generated_key if delivered else None

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except:
            print(f"[Boost] Could not find configured channel {channel_id} in {guild.name}.")
            await _dm_fallback("log channel not found")
            return generated_key if delivered else None

    allowed_roles = [int(r) for r in guild_cfg.get("roles", [])]
    category_id = int(guild_cfg["category_id"]) if guild_cfg.get("category_id") else None

    view = CloseTicketView()

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True, send_messages=True),
    }
    for rid in allowed_roles:
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    category = guild.get_channel(category_id) if category_id else None
    ticket_channel = None
    try:
        ticket_prefix = "boost-claim-test" if is_test else "boost-claim"
        ticket_channel = await guild.create_text_channel(
            name=f"{ticket_prefix}-{member.name}",
            category=category,
            overwrites=overwrites,
            reason=f"Boost claim channel for {member.name}",
        )
        guild_cfg.setdefault("open_boost_tickets", {})[str(ticket_channel.id)] = str(member.id)
        cfg[guild_id_str] = guild_cfg
        save_boost_config(cfg)
    except Exception as e:
        print(f"[Boost] Failed to create ticket channel: {e}")

    # Prepare the log embed for the configured log channel
    log_embed = discord.Embed(
        title="🚀 New Server Boost!" + (" (TEST)" if is_test else ""),
        description=(
            f"💗 Thank you {member.mention} for boosting the server!\n\n"
            f"**Premium Key:**\n```{generated_key if generated_key else 'None currently available. Please contact an admin.'}```\n\n"
            f"[Redeem your key here!](https://discord.com/channels/{guild.id}/1421560929425817662)"
        ),
        color=discord.Color.from_str("#f47fff"),
        timestamp=discord.utils.utcnow()
    )
    if boost_event and boost_event[1] > 1:
        claim = load_boost_config()[str(guild.id)]["key_claims"][str(member.id)]
        status = claim["boost_events"][boost_event[0]]["status"]
        log_embed.add_field(name="Extra Boost Days", value=f"{(boost_event[1] - 1) * claim['boost_events'][boost_event[0]].get('days_per_boost', DEFAULT_DAYS_PER_BOOST)} days: {status}", inline=False)
    log_embed.set_thumbnail(url=member.display_avatar.url)
    log_embed.set_footer(text=f"{guild.name} • Boost count: {guild.premium_subscription_count}")

    if ticket_channel:
        welcome_embed = discord.Embed(
            title="🎁 Boost Reward Ticket" + (" (TEST)" if is_test else ""),
            description=(
                f"Hello {member.mention}! Thank you so much for boosting **{guild.name}**!\n\n"
                f"**Your Premium Key:**\n```{generated_key if generated_key else 'None currently available. Please contact an admin.'}```\n\n"
                "[Redeem your key here!](https://discord.com/channels/1333251917098520628/1421560929425817662)\n\n"
                "Once you're done, you can click **Close Ticket** to delete this channel."
            ),
            color=discord.Color.from_str("#f47fff"),
        )
        welcome_embed.set_thumbnail(url=member.display_avatar.url)
        
        mentions = " ".join(f"<@&{rid}>" for rid in allowed_roles) if allowed_roles else ""
        try:
            await ticket_channel.send(content=f"{member.mention} {mentions}", embed=welcome_embed, view=view)
            delivered = True
            log_embed.add_field(name="Delivery Status", value=f"✅ Key sent in ticket: {ticket_channel.mention}", inline=False)
        except Exception as e:
            print(f"[Boost] Failed to send to ticket channel: {e}")
            log_embed.add_field(name="Delivery Status", value="⚠️ Failed to send to ticket channel", inline=False)
    else:
        if generated_key:
            log_embed.add_field(name="Delivery Status", value="✅ Key provided above!", inline=False)
        else:
            log_embed.add_field(name="Delivery Status", value="❌ Failed to generate key", inline=False)

    # Send the log message to the configured channel
    mentions = " ".join(f"<@&{rid}>" for rid in allowed_roles) if allowed_roles else ""
    try:
        await channel.send(content=f"{member.mention} {mentions}", embed=log_embed)
    except Exception as e:
        print(f"[Boost] Failed to send log message: {e}")

    if generated_key and not delivered:
        await _dm_fallback("no ticket channel")

    return generated_key if delivered else None

async def handle_member_update(before: discord.Member, after: discord.Member):
    """Called from main.py's on_member_update event."""

    # Member state has no per-user boost quantity. Credit only the system message,
    # otherwise its first boost would be rewarded twice across the two event paths.
    if before.premium_since is None and after.premium_since is not None:
        role = after.guild.get_role(BOOST_REWARD_ROLE_ID)
        if role and role not in after.roles:
            try:
                await after.add_roles(role, reason="Started boosting the server")
            except discord.HTTPException:
                print("[Boost] Failed to restore boost role")

    # Boost expired
    elif before.premium_since is not None and after.premium_since is None:
        guild = after.guild
        boost_role = guild.get_role(BOOST_REWARD_ROLE_ID)
        if boost_role and boost_role in after.roles:
            try:
                await after.remove_roles(boost_role, reason="Stopped boosting the server")
            except Exception as e:
                print(f"[Boost] Failed to remove boost role from {after.display_name}: {e}")
        await apply_unboost_penalty(guild, after)


async def handle_member_remove(member: discord.Member):
    """Leaving the server also drops the boost, so it counts as unboosting."""
    if member.premium_since is None:
        return
    if await _was_removed_by_moderator(member):
        return
    await apply_unboost_penalty(member.guild, member)


async def _was_removed_by_moderator(member: discord.Member) -> bool:
    """A kick or ban is not the member unboosting, so it must not trigger the penalty.

    If the audit log can't be read we can't tell, so skip the penalty rather than
    risk banning someone a moderator removed.
    """
    guild = member.guild
    now = discord.utils.utcnow()
    try:
        for action in (discord.AuditLogAction.kick, discord.AuditLogAction.ban):
            async for entry in guild.audit_logs(limit=10, action=action):
                if entry.target is not None and entry.target.id == member.id and (now - entry.created_at).total_seconds() < 60:
                    return True
    except Exception as e:
        print(f"[Boost] Cannot read audit log in {guild.name} ({e}); skipping unboost penalty for {member.id}.")
        return True
    return False


# ── Slash commands ────────────────────────────────────────────────────────────

boost_group = app_commands.Group(name="boost", description="Server boost configuration commands")


@boost_group.command(name="setlog", description="Set the channel where server boost keys will be logged")
@app_commands.describe(channel="The text channel to send logs to")
async def boost_setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = load_boost_config()
    guild_id = str(interaction.guild_id)
    cfg.setdefault(guild_id, {})["channel_id"] = str(channel.id)
    save_boost_config(cfg)
    await interaction.response.send_message(f"✅ Boost logs will now be sent to {channel.mention}", ephemeral=True)


@boost_group.command(name="addrole", description="Add roles to be mentioned when a boost key is logged (up to 5)")
@app_commands.describe(
    role1="1st role to mention", role2="2nd role (optional)", role3="3rd role (optional)",
    role4="4th role (optional)", role5="5th role (optional)",
)
async def boost_addrole(
    interaction: discord.Interaction,
    role1: discord.Role,
    role2: discord.Role = None,
    role3: discord.Role = None,
    role4: discord.Role = None,
    role5: discord.Role = None,
):
    cfg = load_boost_config()
    guild_id = str(interaction.guild_id)
    cfg.setdefault(guild_id, {})
    roles = [str(r) for r in cfg[guild_id].get("roles", [])]
    added, already = [], []
    for r in [role1, role2, role3, role4, role5]:
        if r:
            if str(r.id) not in roles:
                roles.append(str(r.id))
                added.append(r.mention)
            else:
                already.append(r.mention)
    if added:
        cfg[guild_id]["roles"] = roles
        save_boost_config(cfg)
        msg = f"✅ Added {', '.join(added)} to boost notifications."
        if already:
            msg += f"\nℹ️ {', '.join(already)} were already set."
        await interaction.response.send_message(msg, ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ {', '.join(already)} are already set.", ephemeral=True)


@boost_group.command(name="removerole", description="Remove a role from being mentioned on server boosts")
@app_commands.describe(role="The role to remove")
async def boost_removerole(interaction: discord.Interaction, role: discord.Role):
    cfg = load_boost_config()
    guild_id = str(interaction.guild_id)
    if guild_id not in cfg:
        await interaction.response.send_message("❌ No configuration found for this guild.", ephemeral=True)
        return
    roles = [str(r) for r in cfg[guild_id].get("roles", [])]
    if str(role.id) in roles:
        roles.remove(str(role.id))
        cfg[guild_id]["roles"] = roles
        save_boost_config(cfg)
        await interaction.response.send_message(f"✅ Role {role.name} removed from boost notifications.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Role {role.name} is not in the notification list.", ephemeral=True)


@boost_group.command(name="category", description="Set the category where boost claim channels will be created")
@app_commands.describe(category="The category to create claim channels in")
async def boost_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    cfg = load_boost_config()
    guild_id = str(interaction.guild_id)
    cfg.setdefault(guild_id, {})["category_id"] = str(category.id)
    save_boost_config(cfg)
    await interaction.response.send_message(f"✅ Boost claim channels will now be created in **{category.name}**", ephemeral=True)


@boost_group.command(name="config", description="Show the current server boost configuration")
async def boost_config_show(interaction: discord.Interaction):
    cfg = load_boost_config()
    gcfg = cfg.get(str(interaction.guild_id), {})
    channel = interaction.guild.get_channel(int(gcfg.get("channel_id"))) if gcfg.get("channel_id") else None
    category = interaction.guild.get_channel(int(gcfg.get("category_id"))) if gcfg.get("category_id") else None
    roles = [r.mention for r in [interaction.guild.get_role(int(rid)) for rid in gcfg.get("roles", [])] if r]
    embed = discord.Embed(title="🚀 Boost Configuration", color=discord.Color.from_str("#f47fff"))
    embed.add_field(name="Log Channel", value=channel.mention if channel else "None (Falls back to System Channel)", inline=False)
    embed.add_field(name="Ticket Category", value=category.name if category else "None (Created at top)", inline=False)
    embed.add_field(name="Notification Roles", value=", ".join(roles) if roles else "None", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@boost_group.command(name="close", description="Close the current boost claim ticket channel")
@app_commands.checks.has_permissions(manage_channels=True)
async def boost_close(interaction: discord.Interaction):
    """Fallback for closing a boost ticket if its Close Ticket button is unresponsive
    (e.g. a message posted before the bot's persistent view registration was live)."""
    await interaction.response.send_message("🔒 Closing ticket in 3 seconds...")
    cfg = load_boost_config()
    guild_id = str(interaction.guild_id)
    gcfg = cfg.get(guild_id, {})
    open_tickets = gcfg.get("open_boost_tickets", {})
    if str(interaction.channel_id) in open_tickets:
        del open_tickets[str(interaction.channel_id)]
        gcfg["open_boost_tickets"] = open_tickets
        cfg[guild_id] = gcfg
        save_boost_config(cfg)
    await asyncio.sleep(3)
    await interaction.channel.delete(reason=f"Boost ticket closed by {interaction.user}")


@boost_group.command(name="penalty", description="Set what happens when someone who got a boost key removes their boost")
@app_commands.describe(mode="Warn them, or ban them temporarily", days="Ban length in days (ban mode only)")
@app_commands.choices(mode=[
    app_commands.Choice(name="Warn", value="warn"),
    app_commands.Choice(name="Temporary ban", value="ban"),
])
@app_commands.checks.has_permissions(administrator=True)
async def boost_penalty(interaction: discord.Interaction, mode: app_commands.Choice[str], days: app_commands.Range[int, 1, 365] = 30):
    cfg = load_boost_config()
    gcfg = cfg.get(str(interaction.guild_id))
    if gcfg is None:
        await interaction.response.send_message("❌ Set up boost logging first with `/boost setlog`.", ephemeral=True)
        return
    gcfg["penalty"] = {"mode": mode.value, "days": days}
    save_boost_config(cfg)
    detail = f"banned for **{days} days**" if mode.value == "ban" else "**warned**"
    await interaction.response.send_message(f"✅ Members who unboost after getting a key will be {detail}.", ephemeral=True)


@boost_group.command(name="grant", description="Manually give a booster their key (if they never received one)")
@app_commands.describe(member="The booster to give a key to")
@app_commands.checks.has_permissions(administrator=True)
async def boost_grant(interaction: discord.Interaction, member: discord.Member):
    if member.premium_since is None:
        await interaction.response.send_message("❌ That member isn't currently boosting.", ephemeral=True)
        return
    claimed = load_boost_config().get(str(interaction.guild_id), {}).get("key_claims", {})
    if str(member.id) in claimed:
        await interaction.response.send_message("❌ That member already has a recorded reward; pending duration/delivery work retries automatically.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    key = await deliver_boost_reward(interaction.guild, member, is_test=False)
    if key:
        await interaction.followup.send(f"✅ Key delivered to {member.mention}.", ephemeral=True)
    else:
        await interaction.followup.send("❌ Could not generate a key. Check `WEBHOOK_URL` / the key service and the bot console.", ephemeral=True)


@boost_group.command(name="credit", description="Replay a verified Discord boost notification without duplicate credit")
@app_commands.checks.has_permissions(administrator=True)
async def boost_credit(interaction: discord.Interaction, channel: discord.TextChannel, message_id: str):
    if channel.guild.id != interaction.guild_id or not message_id.isdecimal():
        await interaction.response.send_message("Select this server's notification channel and a numeric message ID.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        message = await channel.fetch_message(int(message_id))
    except discord.HTTPException:
        await interaction.followup.send("Notification lookup failed. Check the ID and bot channel access.", ephemeral=True)
        return
    result = await handle_boost_message(message)
    await interaction.followup.send(result, ephemeral=True)


@boost_group.command(name="test", description="Test the server boost webhook and logging system")
@app_commands.checks.has_permissions(administrator=True)
async def boost_test(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        await interaction.followup.send("❌ No WEBHOOK_URL configured in environment.", ephemeral=True)
        return
    await deliver_boost_reward(interaction.guild, interaction.user, is_test=True)
    await interaction.followup.send(f"✅ Webhook test completed! Check your server channels.", ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: discord.ext.commands.Bot):
    bot.tree.add_command(boost_group)
    bot.add_listener(handle_member_update, 'on_member_update')
    bot.add_listener(handle_member_remove, 'on_member_remove')
    bot.add_listener(handle_boost_message, 'on_message')
    bot.add_view(CloseTicketView())

    @tasks.loop(hours=1)
    async def _unban_loop():
        await _expire_temp_bans(bot)

    @tasks.loop(minutes=5)
    async def _revocation_loop():
        await _retry_revocations(bot)
        await _retry_boost_credits(bot)

    async def _start_unban_loop():
        if not _unban_loop.is_running():
            _unban_loop.start()
        if not _revocation_loop.is_running():
            _revocation_loop.start()

    bot.add_listener(_start_unban_loop, 'on_ready')
