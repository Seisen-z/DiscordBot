"""
modules/automod.py
Auto-Moderation – scans messages for scam links / prohibited images and
executes a configurable action (delete, timeout, kick, ban).

Config is read live via load_json so changes take effect
without restarting the bot.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import os
import io
import json
import base64
import aiohttp
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from modules.utils import load_json, save_json

# ── Constants ─────────────────────────────────────────────────────────────────

AUTOMOD_CONFIG_FILE = "automod_configs"

# Known scam / phishing patterns (case-insensitive substring / regex match on domain)
_HARDCODED_SCAM_PATTERNS: list[re.Pattern] = [
    re.compile(r"discord\s*\.?\s*gift", re.I),
    re.compile(r"discordapp\s*\.?\s*gift", re.I),
    re.compile(r"discrord\.", re.I),
    re.compile(r"disc0rd\.", re.I),
    re.compile(r"discorcl\.", re.I),
    re.compile(r"steam\s*community.*free", re.I),
    re.compile(r"free\s*nitro", re.I),
    re.compile(r"nitro\s*giveaway", re.I),
    re.compile(r"claimyournitro\.", re.I),
    re.compile(r"dlscord\.", re.I),
    re.compile(r"dlscordapp\.", re.I),
]

_URL_RE = re.compile(
    r"(https?://[^\s<>\"']+|(?<!\w)(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s<>\"']*)?)",
    re.I,
)

_VISION_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "qwen/qwen2.5-vl-72b-instruct:free",
    "google/gemini-2.0-pro-exp-02-05:free",
    "meta-llama/llama-3.2-11b-vision-instruct:free",
]

# ── Config helpers ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    data = load_json(AUTOMOD_CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_config(data: dict) -> None:
    save_json(AUTOMOD_CONFIG_FILE, data)


def _guild_config(guild_id: int) -> dict:
    return _load_config().get(str(guild_id), {})


def _default_config() -> dict:
    return {
        "enabled": False,
        "action": "delete",
        "timeout_minutes": 10,
        "log_channel_id": None,
        "exempt_role_ids": [],
        "exempt_channel_ids": [],
        "scan_links": True,
        "scan_images": False,
        "blocked_domains": [],
        "whitelist_domains": [],
        "blocked_image_hashes": [],
        "dm_user_on_action": True,
        "dm_message": "Your message was removed because it contained prohibited content (scam/phishing).",
    }


def get_guild_config(guild_id: int) -> dict:
    base = _default_config()
    stored = _guild_config(guild_id)
    base.update(stored)
    return base


def save_guild_config(guild_id: int, cfg: dict) -> None:
    all_cfg = _load_config()
    all_cfg[str(guild_id)] = cfg
    _save_config(all_cfg)


# ── Detection helpers ─────────────────────────────────────────────────────────

def _extract_urls(text: str) -> list[str]:
    return _URL_RE.findall(text)


def _domain_of(url: str) -> str:
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _is_scam_link(url: str, blocked_domains: list[str], whitelist_domains: list[str]) -> bool:
    domain = _domain_of(url).lower()

    # Whitelist check first — whitelisted domains are always safe
    for safe in whitelist_domains:
        safe = safe.strip().lower()
        if safe and (domain == safe or domain.endswith("." + safe)):
            return False

    # Custom blocked domains from config
    for bad in blocked_domains:
        bad = bad.strip().lower()
        if bad and (domain == bad or domain.endswith("." + bad)):
            return True

    return False


_URL_AI_TEXT_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "openrouter/auto",
]

async def _ai_scan_url(url: str) -> bool:
    """Ask a text LLM whether a URL looks like a scam/phishing link.

    Only called for URLs that didn't match any hardcoded pattern or blocked-domain
    list, so the bar is: does the domain/path show clear scam signals?
    Returns True only when the model is confidently YES — defaults to False on
    failure so legitimate links are never silently blocked.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return False

    prompt = (
        "You are a security analyst. Does this URL look like a scam or phishing link?\n"
        f"URL: {url}\n\n"
        "Scam signals to check:\n"
        "- Fake Discord Nitro/gift pages on non-discord.com domains\n"
        "- Free Robux, Steam key, or crypto giveaway sites\n"
        "- Typosquatted domains (dlscord, disc0rd, steamcommunlty, etc.)\n"
        "- Suspicious /claim /free /nitro /gift paths on random domains\n"
        "- IP addresses or unusual TLDs used to mimic known services\n"
        "Reply ONLY 'YES' if it is clearly a scam. Reply ONLY 'NO' if it is safe."
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Seisen88/",
        "X-Title": "Seisen Bot AutoMod",
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12), headers=headers
        ) as session:
            for model in _URL_AI_TEXT_MODELS:
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "max_tokens": 5,
                }
                try:
                    async with session.post(
                        "https://openrouter.ai/api/v1/chat/completions", json=payload,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            content = (result.get("choices", [{}])[0].get("message", {}).get("content") or "").strip().upper()
                            reply = "".join(c for c in content if c.isalpha())
                            print(f"[AutoMod] URL AI ({model}): {reply} | {url[:80]}")
                            if "YES" in reply:
                                return True
                            if "NO" in reply:
                                return False  # First confident NO is enough
                except asyncio.TimeoutError:
                    continue
    except Exception as exc:
        print(f"[AutoMod] URL AI error: {exc}")
    return False


async def _image_sha256(attachment: discord.Attachment) -> tuple[Optional[str], bytes]:
    """Download an image attachment once. Returns (sha256_hex, raw_bytes)."""
    try:
        data = await attachment.read()
        return hashlib.sha256(data).hexdigest(), data
    except Exception:
        return None, b""


async def _ai_scan_image(data: bytes, mime_type: str) -> bool:
    """Use OpenRouter Vision AI to detect scams in images. Accepts pre-read bytes.

    Tries every model in _VISION_MODELS — returns True on the first YES, False
    only after all models have answered NO (or failed).  Stopping at the first
    model's response was the old bug: an unreliable free model saying NO would
    hide a real scam from every subsequent model.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key or len(data) > 8 * 1024 * 1024:
        return False

    b64 = base64.b64encode(data).decode("utf-8")
    prompt = (
        "You are a security moderator. Analyze this image for scams/phishing:\n"
        "1. Fake Discord Nitro gifts from unofficial domains.\n"
        "2. Free Robux/Steam keys requiring a link click.\n"
        "3. Fake verification bots or QR code login scams.\n"
        "4. Impersonation of Discord Staff or security alerts.\n"
        "5. Misspelled URLs (e.g. dlscord, steamcommunlty).\n"
        "Reply EXACTLY 'YES' if ANY indicator is present. Otherwise reply 'NO'."
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Seisen88/",
        "X-Title": "Seisen Bot AutoMod",
    }
    no_count = 0
    tried = 0
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20), headers=headers
        ) as session:
            for model in _VISION_MODELS:
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                    ]}],
                    "temperature": 0.0,
                    "max_tokens": 10,
                }
                try:
                    async with session.post(
                        "https://openrouter.ai/api/v1/chat/completions", json=payload,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            content = (result.get("choices", [{}])[0].get("message", {}).get("content") or "").strip().upper()
                            reply = "".join(c for c in content if c.isalpha())
                            print(f"[AutoMod] Vision AI ({model}): {reply}")
                            tried += 1
                            if "YES" in reply:
                                return True  # Scam confirmed — stop immediately
                            elif "NO" in reply:
                                no_count += 1
                                # After 2 independent NO verdicts, stop to save API calls
                                if no_count >= 2:
                                    return False
                        # 404 = model unavailable, try next
                except asyncio.TimeoutError:
                    continue
    except Exception as exc:
        print(f"[AutoMod] Vision AI error: {exc}")
    return False


# ── Action executor ───────────────────────────────────────────────────────────

async def _execute_action(
    message: discord.Message,
    cfg: dict,
    reason: str,
) -> None:
    """Delete the message and apply the configured punishment."""
    member = message.author
    guild = message.guild
    action = str(cfg.get("action", "delete")).lower()

    # 1. Delete the message
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass

    # 2. DM the user
    if cfg.get("dm_user_on_action", True):
        dm_msg = str(cfg.get("dm_message") or "Your message was removed for containing prohibited content.")
        try:
            await member.send(f"⚠️ **{guild.name}**\n{dm_msg}")
        except Exception:
            pass

    # 3. Punishment
    if member.guild_permissions.administrator:
        # Don't try to ban/kick server admins, just let them know it worked.
        try:
            await member.send(f"*(Developer Note: Your message triggered Auto-Mod and would have resulted in a **{action}**, but you were spared because you are a Server Administrator!)*")
        except Exception:
            pass
    else:
        try:
            if action == "timeout":
                minutes = max(1, int(cfg.get("timeout_minutes", 10)))
                until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
                await member.timeout(until, reason=reason)
            elif action == "kick":
                await member.kick(reason=reason)
            elif action == "ban":
                await member.ban(reason=reason, delete_message_days=0)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[AutoMod] Could not apply action '{action}' to {member}: {e}")

    # 4. Log embed
    log_channel_id = cfg.get("log_channel_id")
    if log_channel_id:
        try:
            log_ch = guild.get_channel(int(log_channel_id))
            if log_ch and hasattr(log_ch, "send"):
                action_labels = {
                    "delete": "🗑️ Message Deleted",
                    "timeout": f"⏱️ Timed Out ({cfg.get('timeout_minutes', 10)} min)",
                    "kick": "👢 Kicked",
                    "ban": "🔨 Banned",
                }
                color_map = {
                    "delete": discord.Color.orange(),
                    "timeout": discord.Color.gold(),
                    "kick": discord.Color.red(),
                    "ban": discord.Color.dark_red(),
                }
                embed = discord.Embed(
                    title=f"🛡️ Auto-Mod Action — {action_labels.get(action, action.title())}",
                    color=color_map.get(action, discord.Color.orange()),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_author(name=str(member), icon_url=member.display_avatar.url)
                embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=True)
                embed.add_field(name="Channel", value=message.channel.mention, inline=True)
                embed.add_field(name="Action", value=action_labels.get(action, action.title()), inline=True)
                embed.add_field(name="Reason", value=reason[:1024], inline=False)
                # Show a preview of the offending message
                content_preview = (message.content or "*(no text)*")[:512]
                embed.add_field(name="Message Preview", value=f"```\n{content_preview}\n```", inline=False)
                embed.set_footer(text=f"User ID: {member.id}")
                await log_ch.send(embed=embed)
        except Exception as log_err:
            print(f"[AutoMod] Log channel error: {log_err}")


# ── Simulation (for dashboard test) ──────────────────────────────────────────

def simulate_detection(text: str, cfg: dict) -> dict:
    """
    Run link detection logic against arbitrary text without taking any action.
    Returns a dict describing what would happen.
    """
    if not cfg.get("enabled", False):
        return {"triggered": False, "reason": "AutoMod is disabled for this server."}

    if not cfg.get("scan_links", True):
        return {"triggered": False, "reason": "Link scanning is disabled."}

    action = cfg.get("action", "delete")

    for pattern in _HARDCODED_SCAM_PATTERNS:
        if pattern.search(text):
            return {
                "triggered": True,
                "reason": f"Scam/prohibited phrase detected in message",
                "url": None,
                "action": action,
            }

    urls = _extract_urls(text)
    blocked_domains = [d.strip().lower() for d in cfg.get("blocked_domains", []) if d.strip()]
    whitelist_domains = [d.strip().lower() for d in cfg.get("whitelist_domains", []) if d.strip()]

    for url in urls:
        if _is_scam_link(url, blocked_domains, whitelist_domains):
            return {
                "triggered": True,
                "reason": f"Scam/prohibited link detected: {url}",
                "url": url,
                "action": action,
            }

    return {"triggered": False, "reason": "No prohibited content detected."}


# ── Event listener ────────────────────────────────────────────────────────────

def _register_listener(bot: commands.Bot) -> None:

    @bot.listen("on_message")
    async def automod_on_message(message: discord.Message) -> None:
        # Ignore DMs, bots, and system messages
        if not message.guild:
            return
        if message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return

        cfg = get_guild_config(message.guild.id)

        if not cfg.get("enabled", False):
            return

        member: discord.Member = message.author

        # Exempt channels
        exempt_channels = [str(c) for c in cfg.get("exempt_channel_ids", [])]
        if str(message.channel.id) in exempt_channels:
            return

        # Exempt roles
        exempt_roles = set(str(r) for r in cfg.get("exempt_role_ids", []))
        member_role_ids = set(str(r.id) for r in member.roles)
        if exempt_roles & member_role_ids:
            return

        # ── Link scanning ─────────────────────────────────────────────────────
        if cfg.get("scan_links", True):
            content = message.content or ""
            
            # Check full message content against hardcoded text patterns first
            for pattern in _HARDCODED_SCAM_PATTERNS:
                if pattern.search(content):
                    reason = f"AutoMod: Scam/phishing phrase detected in message"
                    await _execute_action(message, cfg, reason)
                    return
            
            urls = _extract_urls(content)
            blocked_domains = [d.strip().lower() for d in cfg.get("blocked_domains", []) if d.strip()]
            whitelist_domains = [d.strip().lower() for d in cfg.get("whitelist_domains", []) if d.strip()]
            has_ai_key = bool(os.getenv("OPENROUTER_API_KEY"))

            for url in urls:
                # 1. Fast deterministic check (blocked domain list + whitelist)
                if _is_scam_link(url, blocked_domains, whitelist_domains):
                    reason = f"AutoMod: Scam/phishing link detected — {url[:200]}"
                    await _execute_action(message, cfg, reason)
                    return

                # 2. AI-based check for novel scam domains not in the blocklist
                if has_ai_key:
                    domain = _domain_of(url).lower()
                    # Skip trivially safe domains to save API calls
                    safe_domains = {"discord.com", "discord.gg", "discord.gg", "youtube.com",
                                    "youtu.be", "twitch.tv", "twitter.com", "x.com",
                                    "github.com", "reddit.com", "google.com", "tenor.com",
                                    "giphy.com", "imgur.com", "roblox.com", "steamcommunity.com"}
                    if not any(domain == s or domain.endswith("." + s) for s in safe_domains):
                        if await _ai_scan_url(url):
                            reason = f"AutoMod: AI detected scam/phishing link — {url[:200]}"
                            await _execute_action(message, cfg, reason)
                            return

        # ── Image scanning ────────────────────────────────────────────────────
        if cfg.get("scan_images", False) and message.attachments:
            blocked_hashes = set(h.lower() for h in cfg.get("blocked_image_hashes", []) if h.strip())
            has_ai_key = bool(os.getenv("OPENROUTER_API_KEY"))

            for attachment in message.attachments:
                if not attachment.content_type or not attachment.content_type.startswith("image/"):
                    continue

                # Read the attachment once — reuse bytes for both hash check and AI scan
                sha, img_data = await _image_sha256(attachment)

                # 1. Fast hash check against blocklist
                if sha and sha.lower() in blocked_hashes:
                    await _execute_action(message, cfg, f"AutoMod: Blocked image (hash: {sha[:16]}…)")
                    return

                # 2. AI vision scan using already-read bytes
                if has_ai_key and img_data:
                    mime_type = attachment.content_type or "image/png"
                    if await _ai_scan_image(img_data, mime_type):
                        await _execute_action(message, cfg, "AutoMod: AI detected a scam/phishing image")
                        return


# ── Slash commands ────────────────────────────────────────────────────────────

def _register_commands(bot: commands.Bot) -> None:

    @bot.tree.command(name="automod_status", description="Show the current Auto-Mod configuration for this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_status(interaction: discord.Interaction) -> None:
        cfg = get_guild_config(interaction.guild.id)

        action_labels = {
            "delete": "🗑️ Delete",
            "timeout": f"⏱️ Timeout ({cfg.get('timeout_minutes', 10)} min)",
            "kick": "👢 Kick",
            "ban": "🔨 Ban",
        }

        embed = discord.Embed(
            title="🛡️ Auto-Mod Configuration",
            color=discord.Color.blurple() if cfg.get("enabled") else discord.Color.dark_gray(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Status", value="✅ Enabled" if cfg.get("enabled") else "❌ Disabled", inline=True)
        embed.add_field(name="Action", value=action_labels.get(cfg.get("action", "delete"), cfg.get("action", "delete").title()), inline=True)
        embed.add_field(name="Link Scan", value="✅ On" if cfg.get("scan_links") else "❌ Off", inline=True)
        embed.add_field(name="Image Scan", value="✅ On" if cfg.get("scan_images") else "❌ Off", inline=True)
        embed.add_field(name="Exempt Roles", value=str(len(cfg.get("exempt_role_ids", []))), inline=True)
        embed.add_field(name="Exempt Channels", value=str(len(cfg.get("exempt_channel_ids", []))), inline=True)
        embed.add_field(name="Blocked Domains", value=str(len(cfg.get("blocked_domains", []))), inline=True)
        embed.add_field(name="Whitelisted Domains", value=str(len(cfg.get("whitelist_domains", []))), inline=True)

        log_ch_id = cfg.get("log_channel_id")
        try:
            log_ch = interaction.guild.get_channel(int(log_ch_id)) if log_ch_id else None
        except (TypeError, ValueError):
            log_ch = None
        embed.add_field(name="Log Channel", value=log_ch.mention if log_ch else "*(not set)*", inline=True)

        embed.set_footer(text="Configure via the Seisen Dashboard → Auto Moderation")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="automod_hash", description="Generate the SHA-256 hash of an image for the auto-mod blocklist")
    @app_commands.describe(image="The image file to analyze")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def automod_hash(interaction: discord.Interaction, image: discord.Attachment) -> None:
        await interaction.response.defer(ephemeral=True)

        if not image.content_type or not image.content_type.startswith("image/"):
            return await interaction.followup.send("❌ Please upload a valid image file.", ephemeral=True)

        sha, _ = await _image_sha256(image)
        if not sha:
            return await interaction.followup.send("❌ Failed to process the image. Please try again.", ephemeral=True)

        embed = discord.Embed(
            title="🔍 Image Hash Generated",
            description="Copy the hash below into your **Seisen Dashboard → Auto Moderation** blocklist to automatically delete matching images.",
            color=discord.Color.green(),
        )
        embed.add_field(name="SHA-256 Hash", value=f"`{sha}`", inline=False)
        embed.set_thumbnail(url=image.url)

        await interaction.followup.send(embed=embed, ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: commands.Bot) -> None:
    _register_listener(bot)
    _register_commands(bot)
    print("[System] Auto-Mod module loaded")

