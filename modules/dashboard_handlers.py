"""
modules/dashboard_handlers.py
Handlers for IPC triggers.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands
from pathlib import Path
import urllib.parse
from uuid import uuid4

def resolve_local_asset(url: str, files_list: list) -> str:
    if not url:
        return url
    url = url.strip()
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if path.startswith("/api/bot/assets/") or path.startswith("/api/assets/"):
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 3:
                filename = parts[-1]
                g_id = parts[-2]
                local_path = Path(__file__).resolve().parent.parent / "database" / "assets" / g_id / filename
                if local_path.is_file():
                    safe_name = f"att_{uuid4().hex[:6]}_{filename}"
                    files_list.append(discord.File(str(local_path), filename=safe_name))
                    return f"attachment://{safe_name}"
    except Exception:
        pass
    return url

from modules.utils import load_json, save_json, _as_int
from modules.tickets import load_ticket_config, save_ticket_config, CreateTicketView
from modules.reaction_roles import load_reaction_roles, save_reaction_roles, build_reaction_role_embed
from modules.select_menu_roles import build_select_menu_embed, SelectMenuRoleView, load_select_menu_roles, save_select_menu_roles
# In fully refactored architecture, dashboard actions will invoke functions from modules.
from modules.onboarding import (
    update_onboarding_config,
    get_onboarding_config,
    run_welcome_simulation_trigger,
    normalize_welcome_dynamic_images,
    resolve_member_from_payload,
    render_welcome_dynamic_image_bytes,
)
from modules.polls import (
    send_native_poll,
    clamp_poll_duration_hours,
    normalize_poll_choices,
    upsert_poll_state_entry,
    end_native_poll,
)
from modules.roblox_monitor import fetch_roblox_game, fetch_roblox_thumbnail
from modules.giveaways import create_giveaway, conclude_giveaway, reroll_giveaway, delete_giveaway
from modules.member_counter import create_member_counter_channel, sync_member_counter_now
from modules.social_monitor import (
    fetch_youtube_latest_entry,
    resolve_social_source_url,
    fetch_source_latest_entry,
    build_social_embed,
    build_social_action_view,
    load_social_monitors,
    save_social_monitors,
    _sanitize_social_error,
)
from modules.activity_rewards import manual_reroll_reward, manual_revoke_reward

from modules.sticky import load_sticky, save_sticky

_bot: commands.Bot | None = None

async def resolve_trigger_channel(guild: discord.Guild, payload: dict, key: str = "channel_id") -> tuple[int | None, discord.abc.GuildChannel | None]:
    channel_id = _as_int(payload.get(key))
    if not channel_id:
        return None, None
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            channel = None
    return channel_id, channel


async def on_dashboard_trigger(action: str, guild: discord.Guild, payload: dict):
    print(f"[IPC/Dashboard] Received trigger: {action} for guild {guild.name} ({guild.id})")

    if not isinstance(payload, dict):
        payload = {}

    try:
        if action == "announcement":
            data = payload
            channel_id, channel = await resolve_trigger_channel(guild, data)
            if not channel or not hasattr(channel, "send"):
                return {"status": "error", "action": action, "http_status": 404, "message": f"Channel {channel_id} not found."}

            title = data.get("title", "")
            desc = data.get("description", "")
            msg_content = data.get("content", "")
            
            files = []
            thumb = resolve_local_asset(data.get("thumbnail_url"), files)
            image = resolve_local_asset(data.get("image_url"), files)
            images = [resolve_local_asset(img, files) for img in data.get("images", [])]
            footer = data.get("footer")
            role_id = data.get("ping_role_id")

            embeds = []
            main_embed = discord.Embed(title=title, description=desc, color=discord.Color.from_str("#f47fff"))
            if thumb:
                main_embed.set_thumbnail(url=thumb)
            
            first_image = image
            if first_image:
                main_embed.set_image(url=first_image)
            
            if footer:
                main_embed.set_footer(text=footer)
                
            if title or desc or thumb or first_image or footer:
                embeds.append(main_embed)
            
            parts = []
            if role_id:
                parts.append(f"<@&{role_id}>")
            if msg_content:
                parts.append(msg_content)
                
            for img in images:
                if img and img.strip():
                    if img.startswith("attachment://"):
                        # Already converted to file attachment
                        pass
                    else:
                        parts.append(img.strip())
                    
            final_content = "\n".join(parts)
            
            if not final_content and not embeds and not files:
                return {"status": "error", "action": action, "http_status": 400, "message": "Cannot send empty announcement."}

            await channel.send(content=final_content, embeds=embeds, files=files)
            return {"status": "success", "action": action, "channel_id": str(channel_id)}
                
        elif action == "sticky":
            channel_id, channel = await resolve_trigger_channel(guild, payload)
            if not channel or not hasattr(channel, "send"):
                return {"status": "error", "action": action, "http_status": 404, "message": f"Channel {channel_id} not found."}

            title = payload.get("title")
            content = payload.get("content", "")
            color = payload.get("color", 5793266)
            embed = discord.Embed(description=content, color=discord.Color(color))
            if title:
                embed.title = title

            new_msg = await channel.send(embed=embed)
            
            # Record it so it gets deleted when a normal message forces a repin
            stickies = load_sticky()
            gid = str(guild.id)
            cid = str(channel_id)
            if gid in stickies and cid in stickies[gid] and isinstance(stickies[gid][cid], dict):
                old_id = stickies[gid][cid].get("last_message_id")
                if old_id:
                    try:
                        old_msg = channel.get_partial_message(old_id)
                        await old_msg.delete()
                    except Exception:
                        pass
                stickies[gid][cid]["last_message_id"] = new_msg.id
                save_sticky(stickies)

            return {"status": "success", "action": action, "channel_id": str(channel_id)}
                
        elif action == "ticket":
            channel_id, channel = await resolve_trigger_channel(guild, payload)
            if not channel or not hasattr(channel, "send"):
                return {"status": "error", "action": action, "http_status": 404, "message": f"Channel {channel_id} not found."}

            # Load saved config as fallback, but prefer fields sent in payload
            cfg = load_ticket_config()
            gcfg = cfg.get(str(guild.id), {})

            # Prefer payload fields (sent directly from dashboard Visual editor)
            embed_title = payload.get("embed_title") if "embed_title" in payload else (gcfg.get("embed_title") or "🎫 Support Tickets")
            embed_description = payload.get("embed_description") if "embed_description" in payload else (gcfg.get("embed_description") or "Need help? Click the button below to open a ticket with our staff team.")
            embed_footer = payload.get("embed_footer") if "embed_footer" in payload else gcfg.get("embed_footer")
            embed_thumbnail = payload.get("embed_thumbnail") if "embed_thumbnail" in payload else gcfg.get("embed_thumbnail")
            raw_color = payload.get("embed_color") if "embed_color" in payload else gcfg.get("embed_color", "#5865F2")

            # Parse color — stored as hex string like "#5865F2" or decimal int
            try:
                if isinstance(raw_color, str) and raw_color.startswith("#"):
                    embed_color = discord.Color(int(raw_color.lstrip("#"), 16))
                elif raw_color:
                    embed_color = discord.Color(int(raw_color))
                else:
                    embed_color = discord.Color.blurple()
            except Exception:
                embed_color = discord.Color.blurple()

            panel_embed = discord.Embed(
                title=embed_title,
                description=embed_description,
                color=embed_color,
            )
            if embed_footer:
                panel_embed.set_footer(text=embed_footer)
            if embed_thumbnail:
                panel_embed.set_thumbnail(url=embed_thumbnail)

            # Add the Create Ticket button with a persistent view
            view = CreateTicketView(style=discord.ButtonStyle.primary)
            panel_msg = await channel.send(embed=panel_embed, view=view)

            # Save panel_message_id back to config
            gcfg["panel_channel_id"] = channel.id
            gcfg["panel_message_id"] = panel_msg.id
            cfg[str(guild.id)] = gcfg
            save_ticket_config(cfg)
            return {
                "status": "success",
                "action": action,
                "channel_id": str(channel.id),
                "panel_message_id": str(panel_msg.id),
            }

        elif action == "test_join_guard":
            member = await resolve_member_from_payload(guild, payload, key="user_id")
            if not member:
                member = guild.get_member(guild.owner_id)
            if not member:
                return {
                    "status": "error",
                    "action": action,
                    "http_status": 404,
                    "message": "User not found in guild, and owner not found. Join the server to test this.",
                }
            
            cfg = get_onboarding_config(guild.id)
            reasons = ["Account is too new (Test)", "No custom avatar (Test)"]
            reason_text = "Join Guard: " + " | ".join(reasons)

            # 1. Send the DM embed
            try:
                embed = discord.Embed(
                    title=f"🛑 Blocked from {member.guild.name} (Simulation)",
                    description="You were automatically removed from the server by the **Join Guard** system.",
                    color=0xED4245
                )
                embed.add_field(name="Reason(s)", value="\n".join(f"• {r}" for r in reasons), inline=False)
                await member.send(embed=embed)
            except discord.Forbidden:
                return {
                    "status": "error",
                    "action": action,
                    "http_status": 400,
                    "message": "Your DMs are disabled. Could not send the simulated block message to you.",
                }
            except Exception as e:
                pass

            # 2. Add to logs
            log_channel_id = _as_int(cfg.get("join_guard_log_channel_id"))
            if log_channel_id:
                log_channel = member.guild.get_channel(log_channel_id)
                if log_channel and hasattr(log_channel, "send"):
                    embed = discord.Embed(
                        title="🛡️ Join Guard Triggered (Simulation)",
                        description=f"{member.mention} was removed by join guard.",
                        color=0xED4245,
                        timestamp=datetime.now(timezone.utc),
                    )
                    embed.add_field(name="Action", value=str(cfg.get("join_guard_action") or "kick").upper(), inline=True)
                    embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)
                    embed.add_field(name="Reason", value=reason_text, inline=False)
                    await log_channel.send(embed=embed)

            return {
                "status": "success",
                "action": action,
                "message": "Sent test DM and log message."
            }

        elif action == "simulate_welcome":
            cfg = get_onboarding_config(guild.id)
            sent_count = await run_welcome_simulation_trigger(guild, cfg, payload)
            print(f"[Onboarding] Welcome simulation sent {sent_count} message(s) for guild {guild.id}.")
            return {"status": "success", "action": action, "sent_count": int(sent_count)}

        elif action == "render_welcome_dynamic_image":
            cfg = get_onboarding_config(guild.id)
            dynamic_images = normalize_welcome_dynamic_images(cfg)

            dynamic_image_id = str(payload.get("dynamic_image_id") or "").strip()
            if not dynamic_image_id:
                return {
                    "status": "error",
                    "action": action,
                    "http_status": 400,
                    "message": "Missing dynamic_image_id.",
                }

            dynamic_cfg = dynamic_images.get(dynamic_image_id)
            if not isinstance(dynamic_cfg, dict):
                return {
                    "status": "error",
                    "action": action,
                    "http_status": 404,
                    "message": f"Dynamic image {dynamic_image_id} not found.",
                }

            simulate_member = await resolve_member_from_payload(guild, payload, key="simulate_user_id")
            if simulate_member is None:
                simulate_member = await resolve_member_from_payload(guild, payload, key="user_id")
            if simulate_member is None:
                simulate_member = guild.get_member(guild.owner_id)

            rendered = await render_welcome_dynamic_image_bytes(dynamic_cfg, guild, simulate_member)
            if not rendered:
                return {
                    "status": "error",
                    "action": action,
                    "http_status": 500,
                    "message": "Dynamic image rendering returned no data.",
                }

            return {
                "status": "success",
                "action": action,
                "dynamic_image_id": dynamic_image_id,
                "mime_type": "image/png",
                "image_base64": base64.b64encode(rendered).decode("ascii"),
            }


        elif action == "create_poll":
            question = str(payload.get("question", "")).strip()
            channel_id = _as_int(payload.get("channel_id"))
            duration_hours = clamp_poll_duration_hours(payload.get("duration_hours", 24))
            multiple = bool(payload.get("multiple", False))
            created_by_user_id = _as_int(payload.get("created_by_user_id") or payload.get("user_id"))

            raw_options = payload.get("options", [])
            if isinstance(raw_options, str):
                raw_options = [raw_options]
            if not isinstance(raw_options, list):
                raw_options = []

            options = normalize_poll_choices(raw_options)
            if not question or len(options) < 2 or not channel_id:
                print("[Poll] Invalid create_poll payload from dashboard.")
                return {"status": "error", "action": action, "http_status": 400, "message": "Invalid poll payload. Provide question, channel_id, and at least 2 options."}

            _, channel = await resolve_trigger_channel(guild, payload)
            if not channel or not hasattr(channel, "send"):
                print(f"[Poll] Dashboard create failed: channel {channel_id} not found.")
                return {"status": "error", "action": action, "http_status": 404, "message": f"Channel {channel_id} not found."}

            try:
                sent = await send_native_poll(
                    channel,
                    question=question,
                    choices=options,
                    duration_hours=duration_hours,
                    multiple=multiple,
                )
                upsert_poll_state_entry(
                    guild_id=guild.id,
                    channel_id=channel_id,
                    message=sent,
                    question=question,
                    choices=options,
                    duration_hours=duration_hours,
                    multiple=multiple,
                    created_by_user_id=created_by_user_id,
                )
                print(
                    f"[Poll] Poll {sent.id} posted in guild {guild.id}, "
                    f"channel {channel_id}."
                )
                return {
                    "status": "success",
                    "action": action,
                    "message": "Poll posted.",
                    "message_id": str(sent.id),
                    "jump_url": str(getattr(sent, "jump_url", "") or ""),
                }
            except Exception as e:
                print(f"[Poll] Failed to create poll from dashboard: {e}")
                return {"status": "error", "action": action, "http_status": 500, "message": f"Failed to create poll: {e}"}


        elif action == "end_poll":
            message_id = _as_int(payload.get("message_id"))
            channel_id = _as_int(payload.get("channel_id"))
            ended_by_user_id = _as_int(payload.get("ended_by_user_id") or payload.get("user_id"))

            if not message_id:
                return {"status": "error", "action": action, "http_status": 400, "message": "Invalid poll message_id."}

            success, message, poll_entry = await end_native_poll(
                guild,
                message_id=message_id,
                channel_id=channel_id,
                ended_by_user_id=ended_by_user_id,
                reason="dashboard",
            )

            if not success:
                status_code = 404 if "not found" in str(message).lower() else 400
                return {"status": "error", "action": action, "http_status": status_code, "message": message}

            return {
                "status": "success",
                "action": action,
                "message": message,
                "poll": poll_entry,
            }


        elif action == "create_giveaway":
            channel_id = _as_int(payload.get("channel_id"))
            if not channel_id:
                return {"status": "error", "action": action, "http_status": 400, "message": "Missing channel_id for giveaway."}

            success, message, giveaway = await create_giveaway(
                guild,
                channel_id=channel_id,
                reward_title=str(payload.get("reward_title") or "").strip(),
                reward_description=str(payload.get("reward_description") or "").strip(),
                winner_count=payload.get("winner_count", 1),
                duration_minutes=payload.get("duration_minutes", 60),
                host_id=_as_int(payload.get("host_user_id") or payload.get("host_id") or payload.get("user_id")),
                ping_role_id=_as_int(payload.get("ping_role_id")),
                key_log_channel_id=_as_int(payload.get("key_log_channel_id")),
                emoji=str(payload.get("emoji") or "🎉"),
                key_tier=str(payload.get("key_tier") or "none"),
            )

            if not success:
                return {"status": "error", "action": action, "http_status": 400, "message": message}
            return {"status": "success", "action": action, "message": message, "giveaway": giveaway}


        elif action == "end_giveaway":
            message_id = _as_int(payload.get("message_id"))
            if not message_id:
                return {"status": "error", "action": action, "http_status": 400, "message": "Invalid giveaway message_id."}

            success, message, giveaway = await conclude_giveaway(
                guild,
                message_id,
                ended_by_user_id=_as_int(payload.get("ended_by_user_id") or payload.get("user_id")),
                reason="dashboard",
            )
            if not success:
                status_code = 404 if "not found" in str(message).lower() else 400
                return {"status": "error", "action": action, "http_status": status_code, "message": message}
            return {"status": "success", "action": action, "message": message, "giveaway": giveaway}


        elif action == "reroll_giveaway":
            message_id = _as_int(payload.get("message_id"))
            if not message_id:
                return {"status": "error", "action": action, "http_status": 400, "message": "Invalid giveaway message_id."}

            success, message, giveaway = await reroll_giveaway(
                guild,
                message_id,
                rerolled_by_user_id=_as_int(payload.get("rerolled_by_user_id") or payload.get("user_id")),
                reroll_winner_count=_as_int(payload.get("reroll_winner_count")),
            )
            if not success:
                status_code = 404 if "not found" in str(message).lower() else 400
                return {"status": "error", "action": action, "http_status": status_code, "message": message}
            return {"status": "success", "action": action, "message": message, "giveaway": giveaway}


        elif action == "delete_giveaway":
            message_id = _as_int(payload.get("message_id"))
            if not message_id:
                return {"status": "error", "action": action, "http_status": 400, "message": "Invalid giveaway message_id."}

            success, message, giveaway = await delete_giveaway(
                guild,
                message_id,
                deleted_by_user_id=_as_int(payload.get("deleted_by_user_id") or payload.get("user_id")),
            )
            if not success:
                status_code = 404 if "not found" in str(message).lower() else 400
                return {"status": "error", "action": action, "http_status": status_code, "message": message}
            return {"status": "success", "action": action, "message": message, "giveaway": giveaway}


        elif action == "create_member_counter_channel":
            success, message, entry = await create_member_counter_channel(
                guild,
                channel_type=str(payload.get("channel_type") or "voice"),
                prefix=str(payload.get("prefix") or "Members: "),
                include_bots=bool(payload.get("include_bots", False)),
                category_id=_as_int(payload.get("category_id")),
                created_by_user_id=_as_int(payload.get("created_by_user_id") or payload.get("user_id")),
            )
            if not success or entry is None:
                return {"status": "error", "action": action, "http_status": 400, "message": message}
            return {"status": "success", "action": action, "message": message, "config": entry}


        elif action == "sync_member_counter":
            success, message, entry = await sync_member_counter_now(guild)
            if not success or entry is None:
                status_code = 404 if "no longer exists" in str(message).lower() else 400
                return {"status": "error", "action": action, "http_status": status_code, "message": message}
            return {"status": "success", "action": action, "message": message, "config": entry}

        elif action == "activity_reward_reroll":
            success, message = await manual_reroll_reward(
                guild,
                actor_user_id=_as_int(payload.get("actor_user_id") or payload.get("user_id")),
            )
            if not success:
                return {"status": "error", "action": action, "http_status": 400, "message": message}
            return {"status": "success", "action": action, "message": message}

        elif action == "activity_reward_revoke":
            target_user_id = _as_int(payload.get("target_user_id"))
            if not target_user_id:
                return {"status": "error", "action": action, "http_status": 400, "message": "Missing target_user_id."}
            success, message = await manual_revoke_reward(
                guild,
                int(target_user_id),
                actor_user_id=_as_int(payload.get("actor_user_id") or payload.get("user_id")),
            )
            if not success:
                return {"status": "error", "action": action, "http_status": 400, "message": message}
            return {"status": "success", "action": action, "message": message}

        elif action == "activity_rewards_test_logging":
            all_cfg = load_json("activity_rewards_configs", {})
            cfg = all_cfg.get(str(guild.id), {}) if isinstance(all_cfg, dict) else {}
            log_ch_id = _as_int(cfg.get("logging_channel_id"))

            member = guild.owner
            if not member and guild.members:
                member = guild.members[0]
            
            simulated_ticket_msg = ""
            tier_display = "Test Key"
            if member:
                from modules.activity_rewards import _create_activity_reward_key_ticket, _tier_and_product, _request_key
                tier, product_name = _tier_and_product()
                tier_display = product_name
                try:
                    fetched_key, fetch_err = await _request_key(product_name, tier, cfg)
                    if not fetched_key:
                        simulated_ticket_msg = f"\n\n(Key webhook test failed: {fetch_err})"
                    else:
                        ch, ticket_err = await _create_activity_reward_key_ticket(
                            guild, member, key_value=fetched_key, tier=product_name
                        )
                        if ch:
                            simulated_ticket_msg = f"\n\nAdditionally, a test key ({product_name}) was successfully fetched from your webhook and delivered to {member.mention} in #{ch.name}."
                        else:
                            simulated_ticket_msg = f"\n\n(Key webhook succeeded, but ticket delivery failed: {ticket_err})"
                except Exception as e:
                    simulated_ticket_msg = f"\n\n(Key webhook or ticket delivery threw error: {e})"

            if not log_ch_id:
                return {
                    "status": "success" if member else "error", 
                    "action": action, 
                    "http_status": 200 if member else 400, 
                    "message": f"No logging channel configured.{simulated_ticket_msg}"
                }

            log_channel = guild.get_channel(log_ch_id)
            if not log_channel or not hasattr(log_channel, "send"):
                return {"status": "error", "action": action, "http_status": 404, "message": f"Logging channel not found.{simulated_ticket_msg}"}

            embed = discord.Embed(
                title="Activity Reward Event (TRIAL)",
                description=f"This is a simulated reward event to verify your log formatting.{simulated_ticket_msg}",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Action", value="Simulated Draw", inline=False)
            embed.add_field(name="Winner", value=f"<@{member.id if member else payload.get('user_id', 1234567890)}>", inline=True)
            embed.add_field(name="Tier", value=tier_display, inline=True)
            embed.add_field(name="Reason", value="Random Activity Draw", inline=True)
            embed.set_footer(text="Testing Activity Rewards Logging")
            try:
                await log_channel.send(embed=embed)
                return {"status": "success", "action": action, "message": f"Test message sent to #{log_channel.name}.{simulated_ticket_msg}"}
            except discord.Forbidden:
                return {"status": "error", "action": action, "http_status": 403, "message": f"Missing permissions to send messages in #{log_channel.name}.{simulated_ticket_msg}"}
            except Exception as e:
                return {"status": "error", "action": action, "http_status": 500, "message": f"Discord API error: {e}.{simulated_ticket_msg}"}

        elif action == "roblox":
            universe_id = _as_int(payload.get("universe_id"))
            channel_id = _as_int(payload.get("channel_id"))
            role_id = _as_int(payload.get("role_id"))
            if universe_id and channel_id:
                _, channel = await resolve_trigger_channel(guild, payload)
                if channel and hasattr(channel, "send"):
                    game = await fetch_roblox_game(universe_id)
                    if game:
                        try:
                            thumbnail = await fetch_roblox_thumbnail(universe_id)
                        except Exception:
                            thumbnail = None

                        role = guild.get_role(role_id) if role_id else None

                        embed = discord.Embed(
                            title=f"🧪 FORCE CHECK — {game['name']}",
                            url=f"https://www.roblox.com/games/{game.get('rootPlaceId', universe_id)}",
                            color=0xFFFF00,
                            timestamp=datetime.now(timezone.utc),
                        )
                        embed.add_field(name="🎮 Game", value=game["name"], inline=True)
                        embed.add_field(name="📊 Playing", value=f"{game.get('playing', 0):,}", inline=True)
                        embed.add_field(name="👍 Visits", value=f"{game.get('visits', 0):,}", inline=True)
                        if thumbnail:
                            embed.set_thumbnail(url=thumbnail)
                        embed.set_footer(text="Roblox Game Monitor — Force Check")

                        content = role.mention if role else None
                        await channel.send(content=content, embed=embed)
                        print(
                            f"[Roblox Monitor] Force check sent for universe {universe_id} "
                            f"in guild {guild.id}, channel {channel_id}."
                        )
                        return {"status": "success", "action": action, "channel_id": str(channel_id), "universe_id": str(universe_id)}
                    print(f"[Roblox Monitor] Force check failed: game not found for universe {universe_id}.")
                    return {"status": "error", "action": action, "http_status": 404, "message": f"Game not found for universe {universe_id}."}

                print(f"[Roblox Monitor] Force check failed: channel {channel_id} not found.")
                return {"status": "error", "action": action, "http_status": 404, "message": f"Channel {channel_id} not found."}
            return {"status": "error", "action": action, "http_status": 400, "message": "Missing universe_id or channel_id."}

        elif action == "social_test_post":
            platform = str(payload.get("platform") or "rss").strip().lower()
            source = str(payload.get("source") or "").strip()
            channel_id = _as_int(payload.get("channel_id"))
            role_id = _as_int(payload.get("role_id"))

            if not channel_id:
                return {"status": "error", "action": action, "http_status": 400, "message": "Missing channel_id for social test post."}
            if not source:
                return {"status": "error", "action": action, "http_status": 400, "message": "Missing source for social test post."}

            _, channel = await resolve_trigger_channel(guild, payload)
            if not channel or not hasattr(channel, "send"):
                return {"status": "error", "action": action, "http_status": 404, "message": f"Channel {channel_id} not found."}

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25),
                headers={"User-Agent": "SeisenHubBot/1.0 (+social-monitor)"},
            ) as session:
                if platform == "youtube":
                    resolved_source_url, latest = await fetch_youtube_latest_entry(source, session)
                else:
                    resolved_source_url = resolve_social_source_url(platform, source)
                    if not resolved_source_url:
                        return {
                            "status": "error",
                            "action": action,
                            "http_status": 400,
                            "message": "Could not resolve source URL. Use a valid URL or @handle for TikTok.",
                        }
                    latest = await fetch_source_latest_entry(resolved_source_url, session)

            if latest:
                latest["source_url"] = resolved_source_url

            if not latest:
                return {
                    "status": "error",
                    "action": action,
                    "http_status": 404,
                    "message": "Source resolved but no entries were found yet.",
                }

            role = guild.get_role(role_id) if role_id else None
            content = role.mention if role else None

            monitor_stub = {
                "platform": platform,
                "name": payload.get("name"),
                "source": source,
            }
            embed = build_social_embed(monitor_stub, latest)
            view = build_social_action_view(monitor_stub, latest)
            await channel.send(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
            )

            return {
                "status": "success",
                "action": action,
                "message": "Social test post sent.",
                "resolved_source_url": resolved_source_url,
                "latest_entry_id": str(latest.get("id") or "") or None,
            }

        elif action == "create_reaction_role":
            _, channel = await resolve_trigger_channel(guild, payload)
            if channel and hasattr(channel, "send"):
                title = payload.get("title", "Reaction Roles")
                description = payload.get("description", "React with emojis below to get roles!")
                embed = build_reaction_role_embed(str(title), str(description))

                message = await channel.send(embed=embed)

                # Store the reaction role message
                reaction_roles = load_reaction_roles()
                message_id = str(message.id)

                reaction_roles[message_id] = {
                    "guild_id": guild.id,
                    "channel_id": channel.id,
                    "title": title,
                    "description": description,
                    "roles": {}
                }

                save_reaction_roles(reaction_roles)
                return {"message_id": message_id, "status": "success", "action": action}
            return {"status": "error", "action": action, "http_status": 404, "message": "Target channel not found."}

        elif action == "update_reaction_role":
            message_id = payload.get("message_id")
            if not message_id:
                return {"status": "error", "action": action, "http_status": 400, "message": "Missing message_id."}

            reaction_roles = load_reaction_roles()
            if message_id in reaction_roles:
                message_data = reaction_roles[message_id]

                # Update message data
                if "title" in payload:
                    message_data["title"] = payload["title"]
                if "description" in payload:
                    message_data["description"] = payload["description"]
                if "roles" in payload:
                    message_data["roles"] = payload["roles"]

                save_reaction_roles(reaction_roles)

                # Update the actual Discord message
                try:
                    _, channel = await resolve_trigger_channel(guild, {"channel_id": message_data["channel_id"]})
                    if channel is None:
                        return {"status": "error", "action": action, "http_status": 404, "message": "Target channel not found."}

                    message = await channel.fetch_message(int(message_id))
                    embed = build_reaction_role_embed(
                        str(message_data.get("title") or "Reaction Roles"),
                        str(message_data.get("description") or "React with emojis below to get roles!"),
                        message_data.get("roles") if isinstance(message_data, dict) else None,
                    )

                    await message.edit(embed=embed)

                    # Add any new reactions
                    for emoji_str in message_data["roles"].keys():
                        try:
                            await message.add_reaction(emoji_str)
                        except Exception:
                            pass  # Reaction might already exist

                    return {"status": "success", "action": action}

                except Exception as e:
                    print(f"Failed to update reaction role message: {e}")
                    return {"status": "error", "action": action, "http_status": 500, "message": str(e)}

            return {"status": "error", "action": action, "http_status": 404, "message": "Reaction role message not found."}

        elif action == "create_select_menu_role":
            try:
                message_data = payload.get("message_data", {})

                _, channel = await resolve_trigger_channel(guild, payload)
                if channel and hasattr(channel, "send"):
                    embed = build_select_menu_embed(message_data)

                    # Create select menu view
                    components = message_data.get("components", [])
                    view = SelectMenuRoleView(components, guild.id)

                    # Send the message
                    content = message_data.get("content")
                    sent_message = await channel.send(content=content, embed=embed, view=view)

                    # Store the configuration for persistence after restart
                    select_menu_roles = load_select_menu_roles()
                    select_menu_roles[str(sent_message.id)] = {
                        "guild_id": guild.id,
                        "channel_id": channel.id,
                        "message_data": message_data,
                        "created_by": payload.get("user_id", 0)
                    }
                    save_select_menu_roles(select_menu_roles)

                    # Register the view with the bot's persistent views
                    if _bot is not None:
                        _bot.add_view(view)

                    print(f"[SelectMenuRoles] Posted message {sent_message.id} in channel {channel.id}")
                    return {"message_id": str(sent_message.id), "status": "success", "action": action}

                return {"status": "error", "action": action, "http_status": 404, "message": "Target channel not found."}
                    
            except Exception as e:
                print(f"Failed to create select menu role message: {e}")
                return {"status": "error", "action": action, "http_status": 500, "message": str(e)}

        elif action == "update_select_menu_role":
            try:
                message_id = payload.get("message_id")
                message_data = payload.get("message_data", {})

                if not message_id:
                    return {"status": "error", "action": action, "http_status": 400, "message": "Missing message_id."}

                select_menu_roles = load_select_menu_roles()

                # Resolve channel: prefer local storage, fall back to payload channel_id
                if message_id in select_menu_roles:
                    stored_data = select_menu_roles[message_id]
                    channel_id_to_use = stored_data["channel_id"]
                else:
                    # Message was imported (not originally posted by bot) — use channel_id from payload
                    channel_id_to_use = payload.get("channel_id")
                    stored_data = None

                if not channel_id_to_use:
                    return {"status": "error", "action": action, "http_status": 400, "message": "Missing channel_id. Cannot locate the message without it."}

                _, channel = await resolve_trigger_channel(guild, {"channel_id": channel_id_to_use})
                if not channel:
                    return {"status": "error", "action": action, "http_status": 404, "message": "Target channel not found."}

                try:
                    message = await channel.fetch_message(int(message_id))
                except Exception:
                    return {"status": "error", "action": action, "http_status": 404, "message": "Discord message not found. It may have been deleted."}

                embed = build_select_menu_embed(message_data)
                view = SelectMenuRoleView(message_data.get("components", []), guild.id)
                content = message_data.get("content") or None
                await message.edit(content=content, embed=embed, view=view)

                # Register view for persistence
                if _bot is not None:
                    _bot.add_view(view)

                # Save/update local storage so future updates are faster (fast path)
                if stored_data:
                    stored_data["message_data"] = message_data
                else:
                    select_menu_roles[str(message_id)] = {
                        "guild_id": guild.id,
                        "channel_id": channel_id_to_use,
                        "message_data": message_data,
                        "created_by": payload.get("user_id", 0),
                    }
                save_select_menu_roles(select_menu_roles)

                return {"status": "success", "action": action}

            except Exception as e:
                print(f"Failed to update select menu role message: {e}")
                return {"status": "error", "action": action, "http_status": 500, "message": str(e)}


        elif action == "force_check_social_monitor":
            try:
                monitor_name = str(payload.get("monitor_name") or "").strip()
                monitor_source = str(payload.get("source") or "").strip()

                monitors = await asyncio.to_thread(load_social_monitors)
                guild_cfgs = monitors.get(str(guild.id), [])

                # Find the matching monitor by name or source
                target_cfg = None
                for cfg in guild_cfgs:
                    if monitor_name and str(cfg.get("name") or "").strip() == monitor_name:
                        target_cfg = cfg
                        break
                    if monitor_source and str(cfg.get("source") or "").strip() == monitor_source:
                        target_cfg = cfg
                        break

                if target_cfg is None:
                    return {"status": "error", "action": action, "http_status": 404, "message": "No matching monitor found."}

                platform = str(target_cfg.get("platform") or "rss").strip().lower()
                source = str(target_cfg.get("source") or "").strip()

                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=25),
                    headers={"User-Agent": "SeisenHubBot/1.0 (+social-monitor)"},
                ) as session:
                    if platform == "youtube":
                        resolved_source_url, latest = await fetch_youtube_latest_entry(source, session)
                        latest["source_url"] = resolved_source_url
                    else:
                        source_url = resolve_social_source_url(platform, source)
                        if not source_url:
                            return {"status": "error", "action": action, "http_status": 400, "message": "Could not resolve source URL."}
                        latest = await fetch_source_latest_entry(source_url, session)
                        if latest:
                            latest["source_url"] = source_url

                if not latest:
                    return {"status": "error", "action": action, "http_status": 404, "message": "No entries found in feed."}

                latest_id = str(latest.get("id") or "").strip()
                previous_id = str(target_cfg.get("last_entry_id") or "").strip()

                target_cfg["last_checked_at"] = datetime.now(timezone.utc).isoformat()

                if not previous_id:
                    # First seed — save the ID without posting
                    target_cfg["last_entry_id"] = latest_id
                    await asyncio.to_thread(save_social_monitors, monitors)
                    return {"status": "success", "action": action, "message": "Monitor seeded. No post made (first check).", "latest_id": latest_id}

                if previous_id == latest_id:
                    await asyncio.to_thread(save_social_monitors, monitors)
                    return {"status": "success", "action": action, "message": "No new content. Already up to date.", "latest_id": latest_id}

                # New content — post it
                channel_id = _as_int(target_cfg.get("channel_id"))
                if not channel_id:
                    return {"status": "error", "action": action, "http_status": 400, "message": "No channel configured for this monitor."}

                channel = guild.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await guild.fetch_channel(channel_id)
                    except Exception:
                        channel = None
                if not channel or not hasattr(channel, "send"):
                    return {"status": "error", "action": action, "http_status": 404, "message": "Configured channel not found."}

                role_id = _as_int(target_cfg.get("role_id"))
                role = guild.get_role(role_id) if role_id else None
                content = role.mention if role else None

                embed = build_social_embed(target_cfg, latest)
                view = build_social_action_view(target_cfg, latest)
                await channel.send(
                    content=content,
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
                )

                target_cfg["last_entry_id"] = latest_id
                target_cfg["last_posted_at"] = datetime.now(timezone.utc).isoformat()
                target_cfg["last_error"] = None
                await asyncio.to_thread(save_social_monitors, monitors)

                return {"status": "success", "action": action, "message": f"New content posted: {latest.get('title', latest_id)}", "latest_id": latest_id}

            except Exception as e:
                return {"status": "error", "action": action, "http_status": 500, "message": str(e)}

        elif action == "automod_test":
            text = str(payload.get("text") or "").strip()
            if not text:
                return {"status": "error", "action": action, "http_status": 400, "message": "Missing 'text' in payload."}

            from modules.automod import get_guild_config, simulate_detection
            cfg = get_guild_config(guild.id)
            result = simulate_detection(text, cfg)
            return {"status": "success", "action": action, "result": result}

        elif action == "keypanel_post":
            import os
            channel_id, channel = await resolve_trigger_channel(guild, payload)
            if not channel or not hasattr(channel, "send"):
                return {"status": "error", "action": action, "http_status": 404, "message": f"Channel {channel_id} not found."}

            title = payload.get("title", "Key Generator")
            description = payload.get("description", "Click the button below to generate a key.")
            button_label = payload.get("button_label", "Generate Key")
            required_role_ids = payload.get("required_role_ids", [])
            product_name = payload.get("product_name") or os.getenv("KEY_PANEL_PRODUCT_NAME") or "Premium Key"
            
            # Parse color
            raw_color = payload.get("embed_color")
            try:
                if isinstance(raw_color, str) and raw_color.startswith("#"):
                    embed_color = discord.Color(int(raw_color.lstrip("#"), 16))
                elif raw_color:
                    embed_color = discord.Color(int(raw_color))
                else:
                    embed_color = discord.Color.blue()
            except Exception:
                embed_color = discord.Color.blue()

            embed = discord.Embed(
                title=title,
                description=description,
                color=embed_color,
                timestamp=datetime.now(timezone.utc)
            )

            # Thumbnail
            embed_thumbnail = payload.get("embed_thumbnail")
            if embed_thumbnail:
                embed.set_thumbnail(url=embed_thumbnail)
                
            # Image
            embed_image = payload.get("embed_image")
            if embed_image:
                embed.set_image(url=embed_image)

            # Footer
            if "embed_footer" in payload:
                embed_footer = payload.get("embed_footer")
                if embed_footer and embed_footer.strip():
                    embed.set_footer(text=embed_footer)
            else:
                embed.set_footer(text=f"Product: {product_name}")

            from modules.key_panel import KeyPanelButtonView
            view = KeyPanelButtonView(button_label=button_label)
            
            try:
                panel_msg = await channel.send(embed=embed, view=view)
            except Exception as e:
                return {"status": "error", "action": action, "http_status": 500, "message": f"Failed to send panel: {e}"}

            # Save to key_panels.json
            panels = load_json("key_panels", {})
            guild_id_str = str(guild.id)
            if guild_id_str not in panels:
                panels[guild_id_str] = {}

            panels[guild_id_str][str(panel_msg.id)] = {
                "channel_id": channel.id,
                "message_id": panel_msg.id,
                "title": title,
                "description": description,
                "button_label": button_label,
                "required_role_ids": [str(rid) for rid in required_role_ids],
                "webhook_url": payload.get("webhook_url"),
                "webhook_secret": payload.get("webhook_secret"),
                "webhook_hmac_header": payload.get("webhook_hmac_header"),
                "product_name": payload.get("product_name"),
                "embed_color": payload.get("embed_color"),
                "embed_thumbnail": payload.get("embed_thumbnail"),
                "embed_image": payload.get("embed_image"),
                "embed_footer": payload.get("embed_footer"),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "created_by": payload.get("user_id")
            }
            save_json("key_panels", panels)

            # Register view in bot
            if _bot is not None:
                _bot.add_view(view)

            return {"status": "success", "action": action, "message_id": str(panel_msg.id)}

        elif action == "keypanel_update":
            import os
            message_id = payload.get("message_id")
            if not message_id:
                return {"status": "error", "action": action, "http_status": 400, "message": "Missing message_id."}

            panels = load_json("key_panels", {})
            guild_panels = panels.get(str(guild.id), {})
            if message_id not in guild_panels:
                return {"status": "error", "action": action, "http_status": 404, "message": "Key panel not found."}

            cfg = guild_panels[message_id]
            channel_id = cfg.get("channel_id")
            _, channel = await resolve_trigger_channel(guild, {"channel_id": channel_id})
            if not channel:
                return {"status": "error", "action": action, "http_status": 404, "message": "Channel not found."}

            try:
                message = await channel.fetch_message(int(message_id))
            except Exception:
                return {"status": "error", "action": action, "http_status": 404, "message": "Discord message not found."}

            title = payload.get("title", cfg.get("title"))
            description = payload.get("description", cfg.get("description"))
            button_label = payload.get("button_label", cfg.get("button_label"))
            required_role_ids = payload.get("required_role_ids", cfg.get("required_role_ids", []))
            product_name = payload.get("product_name") or os.getenv("KEY_PANEL_PRODUCT_NAME") or "Premium Key"

            # Parse color
            raw_color = payload.get("embed_color") if "embed_color" in payload else cfg.get("embed_color")
            try:
                if isinstance(raw_color, str) and raw_color.startswith("#"):
                    embed_color = discord.Color(int(raw_color.lstrip("#"), 16))
                elif raw_color:
                    embed_color = discord.Color(int(raw_color))
                else:
                    embed_color = discord.Color.blue()
            except Exception:
                embed_color = discord.Color.blue()

            embed = discord.Embed(
                title=title,
                description=description,
                color=embed_color,
                timestamp=datetime.now(timezone.utc)
            )

            # Thumbnail
            embed_thumbnail = payload.get("embed_thumbnail", cfg.get("embed_thumbnail"))
            if embed_thumbnail:
                embed.set_thumbnail(url=embed_thumbnail)
                
            # Image
            embed_image = payload.get("embed_image", cfg.get("embed_image"))
            if embed_image:
                embed.set_image(url=embed_image)

            # Footer
            if "embed_footer" in payload:
                embed_footer = payload.get("embed_footer")
                if embed_footer and embed_footer.strip():
                    embed.set_footer(text=embed_footer)
            else:
                embed_footer = cfg.get("embed_footer")
                if embed_footer and embed_footer.strip():
                    embed.set_footer(text=embed_footer)
                else:
                    embed.set_footer(text=f"Product: {product_name}")

            from modules.key_panel import KeyPanelButtonView
            view = KeyPanelButtonView(button_label=button_label)

            try:
                await message.edit(embed=embed, view=view)
            except Exception as e:
                return {"status": "error", "action": action, "http_status": 500, "message": f"Failed to edit message: {e}"}

            # Update database
            cfg.update({
                "title": title,
                "description": description,
                "button_label": button_label,
                "required_role_ids": [str(rid) for rid in required_role_ids],
                "webhook_url": payload.get("webhook_url"),
                "webhook_secret": payload.get("webhook_secret"),
                "webhook_hmac_header": payload.get("webhook_hmac_header"),
                "product_name": payload.get("product_name"),
                "embed_color": payload.get("embed_color"),
                "embed_thumbnail": payload.get("embed_thumbnail"),
                "embed_image": payload.get("embed_image"),
                "embed_footer": payload.get("embed_footer"),
            })
            save_json("key_panels", panels)

            # Register view in bot
            if _bot is not None:
                _bot.add_view(view)

            return {"status": "success", "action": action}

        elif action == "keypanel_delete":
            message_id = payload.get("message_id")
            channel_id = payload.get("channel_id")
            if not message_id:
                return {"status": "error", "action": action, "http_status": 400, "message": "Missing message_id."}

            if channel_id:
                _, channel = await resolve_trigger_channel(guild, {"channel_id": channel_id})
                if channel:
                    try:
                        message = await channel.fetch_message(int(message_id))
                        await message.delete()
                    except Exception:
                        pass
            return {"status": "success", "action": action}

        elif action == "apppanel_post":
            channel_id, channel = await resolve_trigger_channel(guild, payload)
            if not channel or not hasattr(channel, "send"):
                return {"status": "error", "action": action, "http_status": 404, "message": f"Channel {channel_id} not found."}

            raw_lcid = payload.get("log_channel_id")
            try:
                log_channel_id = int(raw_lcid) if raw_lcid else None
            except (ValueError, TypeError):
                log_channel_id = None
            title = payload.get("title", "📋 Staff Applications")
            description = payload.get("description", "Click a button below to apply for a staff position in this server.")

            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text="Applications are reviewed by the staff team.")

            from modules.applications import ApplicationPanelView
            view = ApplicationPanelView()
            try:
                panel_msg = await channel.send(embed=embed, view=view)
            except Exception as e:
                return {"status": "error", "action": action, "http_status": 500, "message": f"Failed to send panel: {e}"}

            raw_icid = payload.get("interview_category_id")
            try:
                interview_category_id = int(raw_icid) if raw_icid else None
            except (ValueError, TypeError):
                interview_category_id = None

            panels = load_json("app_panels", {})
            existing = panels.get(str(guild.id), {})
            interviewer_role_ids = [str(r) for r in (payload.get("interviewer_role_ids") or []) if r]

            panels[str(guild.id)] = {
                **existing,
                "channel_id": channel.id,
                "message_id": panel_msg.id,
                "log_channel_id": log_channel_id,
                "interview_category_id": interview_category_id,
                "interviewer_role_ids": interviewer_role_ids,
                "title": title,
                "description": description,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            save_json("app_panels", panels)

            if _bot is not None:
                _bot.add_view(view)

            return {"status": "success", "action": action, "message_id": str(panel_msg.id)}

        elif action == "apppanel_update":
            message_id = payload.get("message_id")
            channel_id = payload.get("channel_id")
            if not message_id or not channel_id:
                return {"status": "error", "action": action, "http_status": 400, "message": "Missing message_id or channel_id."}

            _, channel = await resolve_trigger_channel(guild, {"channel_id": channel_id})
            if not channel:
                return {"status": "error", "action": action, "http_status": 404, "message": f"Channel {channel_id} not found."}

            title = payload.get("title", "📋 Staff Applications")
            description = payload.get("description", "Click a button below to apply for a staff position in this server.")
            raw_lcid = payload.get("log_channel_id")
            try:
                log_channel_id = int(raw_lcid) if raw_lcid else None
            except (ValueError, TypeError):
                log_channel_id = None

            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text="Applications are reviewed by the staff team.")

            from modules.applications import ApplicationPanelView
            view = ApplicationPanelView()
            try:
                msg = await channel.fetch_message(int(message_id))
                await msg.edit(embed=embed, view=view)
            except discord.NotFound:
                return {"status": "error", "action": action, "http_status": 404, "message": "Panel message not found — it may have been deleted. Use Post to create a new one."}
            except Exception as e:
                return {"status": "error", "action": action, "http_status": 500, "message": f"Failed to edit panel: {e}"}

            raw_icid = payload.get("interview_category_id")
            try:
                interview_category_id = int(raw_icid) if raw_icid else None
            except (ValueError, TypeError):
                interview_category_id = None

            interviewer_role_ids = [str(r) for r in (payload.get("interviewer_role_ids") or []) if r]

            panels = load_json("app_panels", {})
            if str(guild.id) in panels:
                panels[str(guild.id)].update({
                    "title": title,
                    "description": description,
                    "log_channel_id": log_channel_id,
                    "interview_category_id": interview_category_id,
                    "interviewer_role_ids": interviewer_role_ids,
                })
                save_json("app_panels", panels)

            if _bot is not None:
                _bot.add_view(view)

            return {"status": "success", "action": action, "message_id": str(message_id)}

        elif action == "apppanel_delete":
            message_id = payload.get("message_id")
            channel_id = payload.get("channel_id")

            if channel_id and message_id:
                _, channel = await resolve_trigger_channel(guild, {"channel_id": channel_id})
                if channel:
                    try:
                        msg = await channel.fetch_message(int(message_id))
                        await msg.delete()
                    except Exception:
                        pass

            panels = load_json("app_panels", {})
            panels.pop(str(guild.id), None)
            save_json("app_panels", panels)
            return {"status": "success", "action": action}

        elif action == "apppanel_assign_role":
            user_id = payload.get("user_id")
            role_id = payload.get("role_id")

            if not user_id or not role_id:
                return {"status": "error", "action": action, "http_status": 400, "message": "Missing user_id or role_id."}

            member = guild.get_member(int(user_id))
            if not member:
                try:
                    member = await guild.fetch_member(int(user_id))
                except Exception:
                    return {"status": "error", "action": action, "http_status": 404, "message": "Member not found in this server."}

            role = guild.get_role(int(role_id))
            if not role:
                return {"status": "error", "action": action, "http_status": 404, "message": "Role not found."}

            try:
                await member.add_roles(role, reason="Application accepted")
            except discord.Forbidden:
                return {"status": "error", "action": action, "http_status": 403, "message": f"Missing permissions to assign role '{role.name}'."}

            return {"status": "success", "action": action, "role_name": role.name}

        elif action == "apppanel_create_channel":
            user_id = payload.get("user_id")
            channel_name = payload.get("channel_name", "interview")
            category_id = payload.get("category_id")
            role_ids = payload.get("role_ids", [])

            if not user_id:
                return {"status": "error", "action": action, "http_status": 400, "message": "Missing user_id."}

            member = guild.get_member(int(user_id))
            if not member:
                try:
                    member = await guild.fetch_member(int(user_id))
                except Exception:
                    return {"status": "error", "action": action, "http_status": 404, "message": "Member not found in this server."}

            category = None
            if category_id:
                cat = guild.get_channel(int(category_id))
                if isinstance(cat, discord.CategoryChannel):
                    category = cat

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                member: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_messages=True),
            }

            safe_name = channel_name.lower().replace(" ", "-")[:100]
            try:
                ch = await guild.create_text_channel(name=safe_name, category=category, overwrites=overwrites)
            except Exception as e:
                return {"status": "error", "action": action, "http_status": 500, "message": f"Failed to create channel: {e}"}

            assigned_roles = []
            for rid in role_ids:
                role = guild.get_role(int(rid))
                if role:
                    try:
                        await member.add_roles(role, reason="Staff application accepted")
                        assigned_roles.append(role.name)
                    except Exception:
                        pass

            try:
                await ch.send(
                    f"Hey {member.mention}! 👋\n\n"
                    f"Thank you for applying to **Seisen Hub**. Our team has reviewed your application and would like to follow up with you here.\n\n"
                    f"Please stay tuned for further instructions!"
                )
            except Exception:
                pass

            return {"status": "success", "action": action, "channel_id": str(ch.id), "channel_name": ch.name, "assigned_roles": assigned_roles}

        return {"status": "error", "action": action, "http_status": 404, "message": f"Unsupported trigger action: {action}"}



    except Exception as e:
        print(f"[IPC/Dashboard] Error processing trigger {action}: {e}")
        return {"status": "error", "action": action, "http_status": 500, "message": str(e)}

def register(bot: commands.Bot):
    global _bot
    _bot = bot
    bot.add_listener(on_dashboard_trigger, "on_dashboard_trigger")
