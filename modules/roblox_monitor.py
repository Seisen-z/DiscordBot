"""
modules/roblox_monitor.py
Scripts to monitor roblox game updates and send discord notifications.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from modules.utils import load_json, save_json, ROBLOX_FILE, ROBLOX_HEALTH_FILE, _as_int


def load_roblox_monitors() -> dict:
    data = load_json(ROBLOX_FILE, {})
    normalized = {}
    for guild_id, cfg in data.items():
        cfgs = [cfg] if isinstance(cfg, dict) else cfg if isinstance(cfg, list) else []
        normalized_cfgs = []
        for entry in cfgs:
            if not isinstance(entry, dict):
                continue
            normalized_cfgs.append({
                **entry,
                "universe_id": _as_int(entry.get("universe_id")),
                "channel_id": _as_int(entry.get("channel_id")),
                "role_id": _as_int(entry.get("role_id")),
            })
        normalized[str(guild_id)] = normalized_cfgs
    return normalized


def save_roblox_monitors(data: dict):
    save_json(ROBLOX_FILE, data)


def load_roblox_health() -> dict:
    return load_json(ROBLOX_HEALTH_FILE, {})


def save_roblox_health(data: dict):
    save_json(ROBLOX_HEALTH_FILE, data)


def set_roblox_health(**updates):
    health = load_roblox_health()
    health.update(updates)
    save_roblox_health(health)


async def fetch_roblox_game(universe_id: int) -> dict | None:
    url = f"https://games.roblox.com/v1/games?universeIds={universe_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            games = data.get("data", [])
            return games[0] if games else None


async def fetch_roblox_thumbnail(universe_id: int) -> str | None:
    url = (
        f"https://thumbnails.roblox.com/v1/games/icons"
        f"?universeIds={universe_id}&size=512x512&format=Png&isCircular=false"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            items = data.get("data", [])
            return items[0].get("imageUrl") if items else None


roblox_group = app_commands.Group(name="roblox", description="Roblox game update monitor")


def _register_commands(bot: discord.ext.commands.Bot):
    @roblox_group.command(name="setup", description="Monitor a Roblox game and ping a role on updates")
    @app_commands.describe(universe_id="The Roblox Universe ID", channel="Channel for embeds", role="Ping role (optional)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def roblox_setup(interaction: discord.Interaction, universe_id: str, channel: discord.TextChannel, role: discord.Role = None):
        await interaction.response.defer(ephemeral=True)
        uid = _as_int(universe_id)
        if not uid:
            await interaction.followup.send("❌ Universe ID must be a number.", ephemeral=True)
            return

        game = await fetch_roblox_game(uid)
        if not game:
            await interaction.followup.send("❌ Could not find that game.", ephemeral=True)
            return

        monitors = load_roblox_monitors()
        guild_id = str(interaction.guild.id)
        monitors.setdefault(guild_id, [])
        
        for cfg in monitors[guild_id]:
            if cfg["universe_id"] == uid:
                await interaction.followup.send("❌ This game is already being monitored.", ephemeral=True)
                return
        
        monitors[guild_id].append({
            "universe_id": uid,
            "channel_id": channel.id,
            "role_id": role.id if role else None,
            "last_updated": game.get("updated"),
        })
        save_roblox_monitors(monitors)
        embed = discord.Embed(title="✅ Roblox Monitor Set Up", color=0x00C851, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Game", value=game["name"], inline=True)
        embed.add_field(name="Universe ID", value=str(uid), inline=True)
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Ping Role", value=role.mention if role else "None", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @roblox_group.command(name="remove", description="Stop monitoring a Roblox game for this server")
    @app_commands.describe(universe_id="Universe ID (optional: removes all if empty)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def roblox_remove(interaction: discord.Interaction, universe_id: str = None):
        monitors = load_roblox_monitors()
        guild_id = str(interaction.guild.id)
        if not monitors.get(guild_id):
            await interaction.response.send_message("❌ No Roblox monitors setup.", ephemeral=True)
            return
        
        if universe_id is None:
            del monitors[guild_id]
            save_roblox_monitors(monitors)
            await interaction.response.send_message("✅ All Roblox monitors removed.", ephemeral=True)
            return
        
        uid = _as_int(universe_id)
        original_length = len(monitors[guild_id])
        monitors[guild_id] = [cfg for cfg in monitors[guild_id] if cfg["universe_id"] != uid]
        if len(monitors[guild_id]) == original_length:
            await interaction.response.send_message("❌ That game is not being monitored.", ephemeral=True)
            return
        
        if not monitors[guild_id]:
            del monitors[guild_id]
        save_roblox_monitors(monitors)
        await interaction.response.send_message("✅ Roblox Game monitor removed.", ephemeral=True)

    @roblox_group.command(name="status", description="Show Roblox monitor configurations")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def roblox_status(interaction: discord.Interaction):
        monitors = load_roblox_monitors()
        cfgs = monitors.get(str(interaction.guild.id), [])
        if not cfgs:
            await interaction.response.send_message("❌ No Roblox monitors configured.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(title="Roblox Monitor Status", color=0x5865F2, timestamp=datetime.now(timezone.utc))
        for cfg in cfgs:
            uid = cfg["universe_id"]
            game = await fetch_roblox_game(uid)
            channel = interaction.guild.get_channel(cfg["channel_id"])
            role = interaction.guild.get_role(cfg.get("role_id")) if cfg.get("role_id") else None
            embed.add_field(
                name=game["name"] if game else f"Universe {uid}",
                value=f"Universe ID: `{uid}`\nChannel: {channel.mention if channel else 'Unknown'}\nRole: {role.mention if role else 'None'}\nLast Update: {cfg.get('last_updated', 'Unknown')}",
                inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @roblox_group.command(name="list", description="List all active monitored Roblox games")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def roblox_list(interaction: discord.Interaction):
        await roblox_status(interaction)

    @roblox_group.command(name="test", description="Send a test notification")
    @app_commands.describe(universe_id="Universe ID")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def roblox_test(interaction: discord.Interaction, universe_id: str):
        uid = _as_int(universe_id)
        monitors = load_roblox_monitors()
        cfgs = monitors.get(str(interaction.guild.id), [])
        cfg = next((c for c in cfgs if c["universe_id"] == uid), None)
        if not cfg:
            await interaction.response.send_message("❌ That game is not being monitored.", ephemeral=True)
            return
        
        game = await fetch_roblox_game(uid)
        channel = interaction.guild.get_channel(cfg["channel_id"])
        if not game or not channel:
            await interaction.response.send_message("❌ Game or channel not found.", ephemeral=True)
            return
        
        role = interaction.guild.get_role(cfg.get("role_id")) if cfg.get("role_id") else None
        try:
            thumbnail = await fetch_roblox_thumbnail(uid)
        except Exception:
            thumbnail = None

        embed = discord.Embed(
            title=f"🧪 TEST UPDATE — {game['name']} — Game Updated!",
            url=f"https://www.roblox.com/games/{game.get('rootPlaceId', uid)}",
            color=0xFFFF00, timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="🎮 Game", value=game["name"], inline=True)
        embed.add_field(name="📊 Playing", value=f"{game.get('playing', 0):,}", inline=True)
        embed.add_field(name="👍 Visits", value=f"{game.get('visits', 0):,}", inline=True)
        embed.add_field(name="⚠️ Note", value="This is a test notification.", inline=False)
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        embed.set_footer(text="Roblox Game Monitor — Test")
        
        await channel.send(content=role.mention if role else None, embed=embed)
        await interaction.response.send_message("✅ Test notification sent!", ephemeral=True)

    bot.tree.add_command(roblox_group)


roblox_update_check_loop = None
_roblox_is_first_run = True

def get_roblox_update_loop(bot_instance: discord.ext.commands.Bot):
    @tasks.loop(minutes=5)
    async def roblox_update_check():
        global _roblox_is_first_run
        started_at = datetime.now(timezone.utc)
        notifications_sent = 0
        monitor_count = 0
        # Run blocking JSON I/O in a thread so the event loop (and Discord heartbeat) stay free.
        await asyncio.to_thread(set_roblox_health, last_poll_started=started_at.isoformat(), last_error=None)

        try:
            monitors = await asyncio.to_thread(load_roblox_monitors)
            changed = False

            for guild_id, cfgs in monitors.items():
                for cfg in cfgs:
                    universe_id = _as_int(cfg.get("universe_id"))
                    if not universe_id:
                        continue
                    monitor_count += 1
                    try:
                        game = await fetch_roblox_game(universe_id)
                    except Exception:
                        continue
                    if not game:
                        continue
                    current_updated = game.get("updated")
                    last_updated = cfg.get("last_updated")

                    is_new_update = False
                    if current_updated and last_updated and current_updated != last_updated:
                        try:
                            def clean_iso(ts: str):
                                ts = ts.replace("Z", "+00:00")
                                if "." in ts:
                                    b, f = ts.split(".", 1)
                                    if "+" in f:
                                        frac, tz = f.split("+", 1)
                                        return f"{b}.{frac[:6]}+{tz}"
                                    if "-" in f:
                                        frac, tz = f.split("-", 1)
                                        return f"{b}.{frac[:6]}-{tz}"
                                return ts

                            dt_curr = datetime.fromisoformat(clean_iso(current_updated))
                            dt_last = datetime.fromisoformat(clean_iso(last_updated))
                            if dt_curr > dt_last:
                                is_new_update = True
                        except Exception:
                            is_new_update = True

                    if is_new_update and not _roblox_is_first_run:
                        guild_int = _as_int(guild_id)
                        channel_id = _as_int(cfg.get("channel_id"))
                        if not guild_int or not channel_id:
                            continue
                        guild = bot_instance.get_guild(guild_int)
                        if not guild:
                            continue
                        channel = guild.get_channel(channel_id)
                        if not channel:
                            continue

                        try:
                            thumbnail = await fetch_roblox_thumbnail(universe_id)
                        except Exception:
                            thumbnail = None

                        role_id = _as_int(cfg.get("role_id"))
                        role = guild.get_role(role_id) if role_id else None

                        embed = discord.Embed(
                            title=f"🔔 {game['name']} — Game Updated!",
                            url=f"https://www.roblox.com/games/{game.get('rootPlaceId', universe_id)}",
                            color=0xFF4444, timestamp=datetime.now(timezone.utc)
                        )
                        embed.add_field(name="🎮 Game", value=game["name"], inline=True)
                        embed.add_field(name="📊 Playing", value=f"{game.get('playing', 0):,}", inline=True)
                        embed.add_field(name="👍 Visits", value=f"{game.get('visits', 0):,}", inline=True)
                        if thumbnail:
                            embed.set_thumbnail(url=thumbnail)
                        embed.set_footer(text="Roblox Game Monitor")

                        try:
                            await channel.send(content=role.mention if role else None, embed=embed)
                            notifications_sent += 1
                        except Exception as e:
                            print(f"[Roblox Monitor] Send update failed for universe {universe_id}: {e}")

                    if current_updated and current_updated != last_updated:
                        cfg["last_updated"] = current_updated
                        changed = True

            if changed:
                await asyncio.to_thread(save_roblox_monitors, monitors)
        except Exception as e:
            await asyncio.to_thread(set_roblox_health, last_error=f"{type(e).__name__}: {e}")
            print(f"[Roblox Monitor] Loop error: {e}")
        finally:
            finished_at = datetime.now(timezone.utc)
            await asyncio.to_thread(
                set_roblox_health,
                last_poll_finished=finished_at.isoformat(),
                last_poll_seconds=round((finished_at - started_at).total_seconds(), 2),
                last_notifications=notifications_sent,
                monitor_count=monitor_count,
            )
            _roblox_is_first_run = False

    @roblox_update_check.before_loop
    async def before_roblox_update_check():
        await bot_instance.wait_until_ready()
        await asyncio.to_thread(
            set_roblox_health,
            loop_started_at=datetime.now(timezone.utc).isoformat(),
            last_error=None,
        )

    return roblox_update_check


def register(bot: discord.ext.commands.Bot):
    global roblox_update_check_loop
    _register_commands(bot)
    if roblox_update_check_loop is None:
        roblox_update_check_loop = get_roblox_update_loop(bot)
