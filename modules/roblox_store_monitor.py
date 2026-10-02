"""
modules/roblox_store_monitor.py
Watches a Roblox group's store (catalog) and posts newly uploaded items to Discord.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from modules.utils import (
    load_json,
    save_json,
    ROBLOX_STORE_FILE,
    ROBLOX_STORE_HEALTH_FILE,
    _as_int,
)


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}
_TIMEOUT = aiohttp.ClientTimeout(total=15)

# How many unseen items a single monitor may announce per cycle; the rest wait
# for the next pass so a bulk upload cannot flood the channel.
_MAX_POSTS_PER_CYCLE = 5
# Items remembered per monitor before the oldest ids are dropped.
_SEEN_HISTORY_LIMIT = 500

_ASSET_TYPE_NAMES = {
    2: "T-Shirt", 8: "Hat", 11: "Shirt", 12: "Pants", 17: "Head", 18: "Face",
    19: "Gear", 41: "Hair Accessory", 42: "Face Accessory", 43: "Neck Accessory",
    44: "Shoulder Accessory", 45: "Front Accessory", 46: "Back Accessory",
    47: "Waist Accessory", 61: "Emote", 64: "T-Shirt Accessory",
    65: "Shirt Accessory", 66: "Pants Accessory", 67: "Jacket Accessory",
    68: "Sweater Accessory", 69: "Shorts Accessory", 72: "Dress/Skirt Accessory",
}


def load_store_monitors() -> dict:
    data = load_json(ROBLOX_STORE_FILE, {})
    normalized = {}
    for guild_id, cfgs in data.items():
        entries = [cfgs] if isinstance(cfgs, dict) else cfgs if isinstance(cfgs, list) else []
        clean = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            clean.append({
                **entry,
                "group_id": _as_int(entry.get("group_id")),
                "channel_id": _as_int(entry.get("channel_id")),
                "role_id": _as_int(entry.get("role_id")),
                "seen_ids": [str(i) for i in entry.get("seen_ids", [])],
                "seeded": bool(entry.get("seeded")),
            })
        normalized[str(guild_id)] = clean
    return normalized


def save_store_monitors(data: dict):
    save_json(ROBLOX_STORE_FILE, data)


def load_store_health() -> dict:
    return load_json(ROBLOX_STORE_HEALTH_FILE, {})


def save_store_health(data: dict):
    save_json(ROBLOX_STORE_HEALTH_FILE, data)


def set_store_health(**updates):
    health = load_store_health()
    health.update(updates)
    save_store_health(health)


# ── Roblox API ────────────────────────────────────────────────────────────────

async def fetch_group_info(session: aiohttp.ClientSession, group_id: int) -> dict | None:
    url = f"https://groups.roblox.com/v1/groups/{group_id}"
    async with session.get(url, headers=_HEADERS, timeout=_TIMEOUT) as resp:
        if resp.status != 200:
            return None
        return await resp.json()


async def fetch_group_store_items(session: aiohttp.ClientSession, group_id: int, limit: int = 30) -> list[dict]:
    """Newest-first list of ``{"id": int, "itemType": "Asset"|"Bundle"}`` for a group's store.

    Roblox only accepts a fixed set of page sizes, so anything else is rounded up
    to the next allowed one and the result trimmed locally.
    """
    page_size = next((allowed for allowed in (10, 28, 30, 60, 120) if allowed >= limit), 120)
    url = (
        "https://catalog.roblox.com/v1/search/items"
        f"?category=All&creatorTargetId={group_id}&creatorType=Group"
        f"&limit={page_size}&sortType=3"
    )
    async with session.get(url, headers=_HEADERS, timeout=_TIMEOUT) as resp:
        if resp.status != 200:
            return []
        data = await resp.json()
        items = [i for i in data.get("data", []) if isinstance(i, dict) and i.get("id")]
        return items[:limit]


async def fetch_asset_details(session: aiohttp.ClientSession, asset_id: int) -> dict | None:
    url = f"https://economy.roblox.com/v2/assets/{asset_id}/details"
    async with session.get(url, headers=_HEADERS, timeout=_TIMEOUT) as resp:
        if resp.status != 200:
            return None
        return await resp.json()


async def fetch_bundle_details(session: aiohttp.ClientSession, bundle_id: int) -> dict | None:
    url = f"https://catalog.roblox.com/v1/bundles/{bundle_id}/details"
    async with session.get(url, headers=_HEADERS, timeout=_TIMEOUT) as resp:
        if resp.status != 200:
            return None
        return await resp.json()


async def fetch_item_thumbnail(session: aiohttp.ClientSession, item_id: int, item_type: str) -> str | None:
    if item_type == "Bundle":
        url = f"https://thumbnails.roblox.com/v1/bundles/thumbnails?bundleIds={item_id}&size=420x420&format=Png&isCircular=false"
    else:
        url = f"https://thumbnails.roblox.com/v1/assets?assetIds={item_id}&size=420x420&format=Png&isCircular=false"
    async with session.get(url, headers=_HEADERS, timeout=_TIMEOUT) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        items = data.get("data", [])
        if not items:
            return None
        entry = items[0]
        return entry.get("imageUrl") if entry.get("state") == "Completed" else None


async def build_item_payload(session: aiohttp.ClientSession, item: dict) -> dict | None:
    """Flatten an Asset or Bundle into the single shape the embed builder expects."""
    item_id = _as_int(item.get("id"))
    item_type = item.get("itemType") or "Asset"
    if not item_id:
        return None

    if item_type == "Bundle":
        details = await fetch_bundle_details(session, item_id)
        if not details:
            return None
        product = details.get("product") or {}
        return {
            "id": item_id,
            "type_label": details.get("bundleType") or "Bundle",
            "name": details.get("name") or "Unknown Bundle",
            "description": details.get("description") or "",
            "price": product.get("priceInRobux"),
            "for_sale": bool(product.get("isForSale")),
            "created": None,
            "url": f"https://www.roblox.com/bundles/{item_id}/",
        }

    details = await fetch_asset_details(session, item_id)
    if not details:
        return None
    return {
        "id": item_id,
        "type_label": _ASSET_TYPE_NAMES.get(details.get("AssetTypeId"), details.get("ProductType") or "Item"),
        "name": details.get("Name") or "Unknown Item",
        "description": details.get("Description") or "",
        "price": details.get("PriceInRobux"),
        "for_sale": bool(details.get("IsForSale")),
        "created": details.get("Created"),
        "url": f"https://www.roblox.com/catalog/{item_id}/",
    }


def _format_price(payload: dict) -> str:
    if not payload.get("for_sale"):
        return "Off-sale"
    price = payload.get("price")
    if price is None:
        return "Not for Robux"
    return "Free" if price == 0 else f"R$ {price:,}"


def build_store_embed(payload: dict, group_name: str, thumbnail: str | None, *, test: bool = False) -> discord.Embed:
    description = payload.get("description") or "*No description provided.*"
    if len(description) > 3800:
        description = description[:3797] + "..."

    embed = discord.Embed(
        title=f"{'🧪 TEST — ' if test else '🛒 '}{payload['name']}",
        url=payload["url"],
        description=description,
        color=0xFFD700 if test else 0x00A2FF,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name=f"New item in {group_name}'s store")
    embed.add_field(name="💰 Price", value=_format_price(payload), inline=True)
    embed.add_field(name="🏷️ Type", value=str(payload.get("type_label") or "Item"), inline=True)
    embed.add_field(name="🆔 Item ID", value=str(payload["id"]), inline=True)

    created = payload.get("created")
    if created:
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00").split(".")[0] + "+00:00")
            embed.add_field(name="📅 Uploaded", value=f"<t:{int(ts.timestamp())}:R>", inline=True)
        except (ValueError, AttributeError):
            pass

    embed.add_field(name="🔗 Link", value=f"[View on Roblox]({payload['url']})", inline=False)
    if thumbnail:
        embed.set_image(url=thumbnail)
    embed.set_footer(text="Roblox Group Store Monitor" + (" — Test" if test else ""))
    return embed


async def announce_item(
    channel: discord.abc.Messageable,
    session: aiohttp.ClientSession,
    item: dict,
    group_name: str,
    role: discord.Role | None,
    *,
    test: bool = False,
) -> bool:
    payload = await build_item_payload(session, item)
    if not payload:
        return False
    try:
        thumbnail = await fetch_item_thumbnail(session, payload["id"], item.get("itemType") or "Asset")
    except Exception:
        thumbnail = None
    embed = build_store_embed(payload, group_name, thumbnail, test=test)
    await channel.send(content=role.mention if role else None, embed=embed)
    return True


# ── Slash commands ────────────────────────────────────────────────────────────

store_group = app_commands.Group(name="robloxstore", description="Roblox group store upload monitor")


def _register_commands(bot: discord.ext.commands.Bot):
    @store_group.command(name="setup", description="Announce new Roblox group store uploads in a channel")
    @app_commands.describe(
        group_id="The Roblox Group ID",
        channel="Channel for the announcements",
        role="Role to ping on each new item (optional)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def store_setup(interaction: discord.Interaction, group_id: str, channel: discord.TextChannel, role: discord.Role = None):
        await interaction.response.defer(ephemeral=True)
        gid = _as_int(group_id)
        if not gid:
            await interaction.followup.send("❌ Group ID must be a number.", ephemeral=True)
            return

        monitors = load_store_monitors()
        guild_key = str(interaction.guild.id)
        monitors.setdefault(guild_key, [])
        if any(cfg["group_id"] == gid for cfg in monitors[guild_key]):
            await interaction.followup.send("❌ That group's store is already being monitored.", ephemeral=True)
            return

        async with aiohttp.ClientSession() as session:
            info = await fetch_group_info(session, gid)
            if not info:
                await interaction.followup.send("❌ Could not find that Roblox group.", ephemeral=True)
                return
            try:
                existing = await fetch_group_store_items(session, gid)
            except Exception:
                existing = []

        # Seed with what is already on sale so setup does not replay the backlog.
        monitors[guild_key].append({
            "group_id": gid,
            "group_name": info.get("name") or str(gid),
            "channel_id": channel.id,
            "role_id": role.id if role else None,
            "seen_ids": [str(i["id"]) for i in existing],
            "seeded": True,
            "last_checked": datetime.now(timezone.utc).isoformat(),
        })
        save_store_monitors(monitors)

        embed = discord.Embed(title="✅ Store Monitor Set Up", color=0x00C851, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Group", value=info.get("name") or str(gid), inline=True)
        embed.add_field(name="Group ID", value=str(gid), inline=True)
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Ping Role", value=role.mention if role else "None", inline=True)
        embed.add_field(name="Baseline", value=f"{len(existing)} existing item(s) ignored", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @store_group.command(name="remove", description="Stop monitoring a Roblox group store")
    @app_commands.describe(group_id="Group ID (optional: removes all if empty)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def store_remove(interaction: discord.Interaction, group_id: str = None):
        monitors = load_store_monitors()
        guild_key = str(interaction.guild.id)
        if not monitors.get(guild_key):
            await interaction.response.send_message("❌ No store monitors configured.", ephemeral=True)
            return

        if group_id is None:
            del monitors[guild_key]
            save_store_monitors(monitors)
            await interaction.response.send_message("✅ All store monitors removed.", ephemeral=True)
            return

        gid = _as_int(group_id)
        before = len(monitors[guild_key])
        monitors[guild_key] = [cfg for cfg in monitors[guild_key] if cfg["group_id"] != gid]
        if len(monitors[guild_key]) == before:
            await interaction.response.send_message("❌ That group is not being monitored.", ephemeral=True)
            return
        if not monitors[guild_key]:
            del monitors[guild_key]
        save_store_monitors(monitors)
        await interaction.response.send_message("✅ Store monitor removed.", ephemeral=True)

    @store_group.command(name="status", description="Show Roblox store monitor configurations")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def store_status(interaction: discord.Interaction):
        monitors = load_store_monitors()
        cfgs = monitors.get(str(interaction.guild.id), [])
        if not cfgs:
            await interaction.response.send_message("❌ No store monitors configured.", ephemeral=True)
            return
        embed = discord.Embed(title="Roblox Store Monitor Status", color=0x5865F2, timestamp=datetime.now(timezone.utc))
        for cfg in cfgs:
            channel = interaction.guild.get_channel(cfg["channel_id"])
            role = interaction.guild.get_role(cfg["role_id"]) if cfg.get("role_id") else None
            embed.add_field(
                name=cfg.get("group_name") or f"Group {cfg['group_id']}",
                value=(
                    f"Group ID: `{cfg['group_id']}`\n"
                    f"Channel: {channel.mention if channel else 'Unknown'}\n"
                    f"Role: {role.mention if role else 'None'}\n"
                    f"Tracked items: {len(cfg.get('seen_ids', []))}\n"
                    f"Last checked: {cfg.get('last_checked', 'Never')}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @store_group.command(name="test", description="Post the group's newest store item as a test")
    @app_commands.describe(group_id="Group ID")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def store_test(interaction: discord.Interaction, group_id: str):
        await interaction.response.defer(ephemeral=True)
        gid = _as_int(group_id)
        cfgs = load_store_monitors().get(str(interaction.guild.id), [])
        cfg = next((c for c in cfgs if c["group_id"] == gid), None)
        if not cfg:
            await interaction.followup.send("❌ That group is not being monitored.", ephemeral=True)
            return

        channel = interaction.guild.get_channel(cfg["channel_id"])
        if not channel:
            await interaction.followup.send("❌ Configured channel no longer exists.", ephemeral=True)
            return

        role = interaction.guild.get_role(cfg["role_id"]) if cfg.get("role_id") else None
        async with aiohttp.ClientSession() as session:
            items = await fetch_group_store_items(session, gid, limit=1)
            if not items:
                await interaction.followup.send("❌ That group's store has no items.", ephemeral=True)
                return
            posted = await announce_item(channel, session, items[0], cfg.get("group_name") or str(gid), role, test=True)

        if posted:
            await interaction.followup.send("✅ Test announcement sent!", ephemeral=True)
        else:
            await interaction.followup.send("❌ Could not load that item's details from Roblox.", ephemeral=True)

    bot.tree.add_command(store_group)


# ── Background loop ───────────────────────────────────────────────────────────

store_update_check_loop = None


def get_store_update_loop(bot_instance: discord.ext.commands.Bot):
    @tasks.loop(minutes=5)
    async def store_update_check():
        started_at = datetime.now(timezone.utc)
        notifications_sent = 0
        monitor_count = 0
        await asyncio.to_thread(set_store_health, last_poll_started=started_at.isoformat(), last_error=None)

        try:
            monitors = await asyncio.to_thread(load_store_monitors)
        except Exception as e:
            print(f"[Store Monitor] Failed to load monitors: {e}")
            await asyncio.to_thread(set_store_health, last_error=f"{type(e).__name__}: {e}")
            return

        changed = False
        try:
            async with aiohttp.ClientSession() as session:
                for guild_id, cfgs in monitors.items():
                    guild = bot_instance.get_guild(_as_int(guild_id) or 0)
                    if not guild:
                        continue
                    for cfg in cfgs:
                        group_id = cfg.get("group_id")
                        channel = guild.get_channel(cfg.get("channel_id") or 0)
                        if not group_id or not channel:
                            continue
                        monitor_count += 1

                        try:
                            items = await fetch_group_store_items(session, group_id)
                        except Exception as e:
                            print(f"[Store Monitor] Fetch failed for group {group_id}: {e}")
                            continue

                        cfg["last_checked"] = datetime.now(timezone.utc).isoformat()
                        changed = True
                        if not items:
                            continue

                        # A monitor added from the dashboard has no baseline yet.
                        # Record the current store once instead of announcing all of it.
                        if not cfg.get("seeded"):
                            cfg["seen_ids"] = [str(i["id"]) for i in items]
                            cfg["seeded"] = True
                            print(f"[Store Monitor] Seeded baseline of {len(items)} item(s) for group {group_id}.")
                            continue

                        seen = set(cfg.get("seen_ids", []))
                        # Oldest first so a burst of uploads posts in upload order.
                        new_items = [i for i in reversed(items) if str(i["id"]) not in seen]
                        if not new_items:
                            continue

                        role_id = cfg.get("role_id")
                        role = guild.get_role(role_id) if role_id else None
                        group_name = cfg.get("group_name") or str(group_id)

                        for item in new_items[:_MAX_POSTS_PER_CYCLE]:
                            try:
                                posted = await announce_item(channel, session, item, group_name, role)
                            except discord.HTTPException as e:
                                print(f"[Store Monitor] Send failed for item {item['id']}: {e}")
                                break
                            except Exception as e:
                                print(f"[Store Monitor] Item {item['id']} failed: {e}")
                                continue
                            if posted:
                                notifications_sent += 1
                                cfg.setdefault("seen_ids", []).append(str(item["id"]))

                        if len(cfg.get("seen_ids", [])) > _SEEN_HISTORY_LIMIT:
                            cfg["seen_ids"] = cfg["seen_ids"][-_SEEN_HISTORY_LIMIT:]
        except Exception as e:
            await asyncio.to_thread(set_store_health, last_error=f"{type(e).__name__}: {e}")
            print(f"[Store Monitor] Loop error: {e}")
        finally:
            if changed:
                try:
                    await asyncio.to_thread(save_store_monitors, monitors)
                except Exception as e:
                    print(f"[Store Monitor] Failed to save monitors: {e}")
            finished_at = datetime.now(timezone.utc)
            await asyncio.to_thread(
                set_store_health,
                last_poll_finished=finished_at.isoformat(),
                last_poll_seconds=round((finished_at - started_at).total_seconds(), 2),
                last_notifications=notifications_sent,
                monitor_count=monitor_count,
            )

    @store_update_check.before_loop
    async def before_store_update_check():
        await bot_instance.wait_until_ready()
        await asyncio.to_thread(
            set_store_health,
            loop_started_at=datetime.now(timezone.utc).isoformat(),
            last_error=None,
        )

    return store_update_check


def register(bot: discord.ext.commands.Bot):
    global store_update_check_loop
    _register_commands(bot)
    if store_update_check_loop is None:
        store_update_check_loop = get_store_update_loop(bot)
