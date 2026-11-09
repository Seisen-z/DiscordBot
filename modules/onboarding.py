"""
modules/onboarding.py
Onboarding system extracted.
"""

from __future__ import annotations
import io
import os, json, re, random
import ssl
from datetime import datetime, timezone, timedelta

import aiohttp
import certifi
import discord
try:
    from PIL import Image, ImageFont, ImageDraw, ImageOps
except ImportError:
    Image = None
    ImageFont = None
    ImageDraw = None
    ImageOps = None
from modules.utils import _as_int, load_json, save_json

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_DYNAMIC_IMAGE_RUNTIME_WARNING_EMITTED = False

ONBOARDING_CONFIG_FILE = "onboarding_configs"


def default_onboarding_config() -> dict:
    return {
        "enabled": False,
        "verified_role_id": None,
        "auto_role_ids": [],
        "welcome_enabled": True,
        "welcome_channel_id": None,
        "welcome_content": "",
        "welcome_embed_title": "Welcome ${userglobalnickname}!",
        "welcome_embed_description": "to ${guildname}\n\nYou are member #${guildmembercount}.",
        "welcome_embed_color": "#5865F2",
        "welcome_embed_thumbnail": None,
        "welcome_embed_image": None,
        "welcome_embed_footer": "Enjoy your stay",
        "welcome_message_groups": [],
        "welcome_messages": [],
        "welcome_dynamic_images": [],
        "send_welcome_on_join": False,
        "join_guard_enabled": False,
        "min_account_age_days": 0,
        "block_default_avatar": False,
        "join_guard_action": "kick",
        "join_guard_log_channel_id": None,
    }


def load_onboarding_config() -> dict:
    data = load_json(ONBOARDING_CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def save_onboarding_config(data: dict):
    save_json(ONBOARDING_CONFIG_FILE, data)


def get_onboarding_config(guild_id: int | str) -> dict:
    raw = load_onboarding_config().get(str(guild_id), {})
    cfg = default_onboarding_config()
    if isinstance(raw, dict):
        cfg.update(raw)

    cfg["auto_role_ids"] = [str(v) for v in (cfg.get("auto_role_ids") or []) if str(v).strip()]
    cfg["welcome_message_groups"] = cfg.get("welcome_message_groups") if isinstance(cfg.get("welcome_message_groups"), list) else []
    cfg["welcome_messages"] = cfg.get("welcome_messages") if isinstance(cfg.get("welcome_messages"), list) else []
    cfg["welcome_dynamic_images"] = cfg.get("welcome_dynamic_images") if isinstance(cfg.get("welcome_dynamic_images"), list) else []
    return cfg


def update_onboarding_config(guild_id: int | str, updater: dict):
    all_cfg = load_onboarding_config()
    existing = get_onboarding_config(guild_id)
    existing.update(updater or {})
    all_cfg[str(guild_id)] = existing
    save_onboarding_config(all_cfg)


def parse_embed_color(raw: str | int | None, default_color: int = 0x5865F2) -> discord.Color:
    try:
        if isinstance(raw, str) and raw.strip().startswith("#"):
            return discord.Color(int(raw.strip().lstrip("#"), 16))
        if raw not in (None, ""):
            return discord.Color(int(raw))
    except Exception:
        pass
    return discord.Color(default_color)


def render_onboarding_text(template: str | None, guild: discord.Guild, member: discord.Member | None = None) -> str:
    text = str(template or "")
    if not text:
        return ""

    member_name = "member"
    global_name = "member"
    mention = "@member"
    user_id = "0"

    if member is not None:
        member_name = member.name
        global_name = member.global_name or member.display_name or member.name
        mention = member.mention
        user_id = str(member.id)

    replacements = {
        "${guildname}": guild.name,
        "${guildmembercount}": str(guild.member_count or 0),
        "${membercount}": str(guild.member_count or 0),
        "${username}": member_name,
        "${userglobalnickname}": global_name,
        "${usermention}": mention,
        "${userid}": user_id,
    }

    for token, value in replacements.items():
        text = text.replace(token, value)

    return text


def build_welcome_embed(cfg: dict, guild: discord.Guild, member: discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title=render_onboarding_text(cfg.get("welcome_embed_title"), guild, member),
        description=render_onboarding_text(cfg.get("welcome_embed_description"), guild, member),
        color=parse_embed_color(cfg.get("welcome_embed_color"), 0x5865F2),
        timestamp=datetime.now(timezone.utc),
    )

    thumbnail_url = render_onboarding_text(cfg.get("welcome_embed_thumbnail"), guild, member)
    if thumbnail_url.startswith("http"):
        embed.set_thumbnail(url=thumbnail_url)

    image_url = render_onboarding_text(cfg.get("welcome_embed_image"), guild, member)
    if image_url.startswith("http"):
        embed.set_image(url=image_url)

    footer_text = render_onboarding_text(cfg.get("welcome_embed_footer"), guild, member)
    if footer_text:
        embed.set_footer(text=footer_text)

    return embed


def normalize_welcome_message_groups(cfg: dict) -> list[dict]:
    groups: list[dict] = []
    raw_groups = cfg.get("welcome_message_groups")
    if isinstance(raw_groups, list):
        for index, entry in enumerate(raw_groups):
            if not isinstance(entry, dict):
                continue
            groups.append({
                "id": str(entry.get("id") or f"group-{index + 1}"),
                "name": str(entry.get("name") or f"Group {index + 1}"),
                "channel_id": str(entry.get("channel_id") or ""),
                "enabled": bool(entry.get("enabled", True)),
            })

    if not groups:
        groups = [{
            "id": "group-main",
            "name": "Group 1",
            "channel_id": str(cfg.get("welcome_channel_id") or ""),
            "enabled": True,
        }]

    return groups


def normalize_welcome_messages(cfg: dict, groups: list[dict]) -> list[dict]:
    messages: list[dict] = []
    group_ids = {str(group.get("id")) for group in groups}
    fallback_group_id = groups[0]["id"] if groups else "group-main"

    raw_messages = cfg.get("welcome_messages")
    if isinstance(raw_messages, list):
        for index, entry in enumerate(raw_messages):
            if not isinstance(entry, dict):
                continue

            group_id = str(entry.get("group_id") or fallback_group_id)
            if group_id not in group_ids:
                group_id = fallback_group_id

            try:
                weight = max(1, int(entry.get("weight") or 1))
            except (TypeError, ValueError):
                weight = 1

            dynamic_image_id = str(entry.get("dynamic_image_id") or "")
            mode_raw = str(entry.get("message_mode") or "").strip().lower()
            if mode_raw in {"normal", "embed"}:
                message_mode = mode_raw
            else:
                message_mode = "normal" if dynamic_image_id else "embed"

            messages.append({
                "id": str(entry.get("id") or f"message-{index + 1}"),
                "name": str(entry.get("name") or f"Message {index + 1}"),
                "group_id": group_id,
                "weight": weight,
                "enabled": bool(entry.get("enabled", True)),
                "message_mode": message_mode,
                "content": str(entry.get("content") or ""),
                "embed_title": str(entry.get("embed_title") or cfg.get("welcome_embed_title") or ""),
                "embed_description": str(entry.get("embed_description") or cfg.get("welcome_embed_description") or ""),
                "embed_color": str(entry.get("embed_color") or cfg.get("welcome_embed_color") or "#5865F2"),
                "embed_thumbnail": str(entry.get("embed_thumbnail") or ""),
                "embed_image": str(entry.get("embed_image") or ""),
                "embed_footer": str(entry.get("embed_footer") or cfg.get("welcome_embed_footer") or ""),
                "dynamic_image_id": dynamic_image_id,
            })

    if not messages:
        messages = [{
            "id": "message-main",
            "name": "Message 1",
            "group_id": fallback_group_id,
            "weight": 1,
            "enabled": True,
            "message_mode": "embed",
            "content": str(cfg.get("welcome_content") or ""),
            "embed_title": str(cfg.get("welcome_embed_title") or ""),
            "embed_description": str(cfg.get("welcome_embed_description") or ""),
            "embed_color": str(cfg.get("welcome_embed_color") or "#5865F2"),
            "embed_thumbnail": str(cfg.get("welcome_embed_thumbnail") or ""),
            "embed_image": str(cfg.get("welcome_embed_image") or ""),
            "embed_footer": str(cfg.get("welcome_embed_footer") or ""),
            "dynamic_image_id": "",
        }]

    return messages


def normalize_welcome_dynamic_images(cfg: dict) -> dict[str, dict]:
    images_by_id: dict[str, dict] = {}
    raw_images = cfg.get("welcome_dynamic_images")
    if not isinstance(raw_images, list):
        return images_by_id

    for index, entry in enumerate(raw_images):
        if not isinstance(entry, dict):
            continue
        image_id = str(entry.get("id") or f"image-{index + 1}")
        image_width = _safe_int(entry.get("width"), 500, 128, 4000)
        image_height = _safe_int(entry.get("height"), 350, 128, 4000)
        background_color = str(entry.get("background_color") or "#0E1824").strip() or "#0E1824"
        allow_transparent_background = bool(entry.get("allow_transparent_background"))

        raw_layers = entry.get("layers")
        normalized_layers: list[dict] = []
        if isinstance(raw_layers, list):
            for layer_index, raw_layer in enumerate(raw_layers):
                if not isinstance(raw_layer, dict):
                    continue

                layer_type = str(raw_layer.get("type") or "text").strip().lower()
                if layer_type not in {"text", "avatar", "block", "logo"}:
                    layer_type = "text"

                z_position = str(raw_layer.get("z_position") or ("back" if layer_type == "block" else "front")).strip().lower()
                if z_position not in {"back", "front"}:
                    z_position = "front"

                text_align = str(raw_layer.get("text_align") or "left").strip().lower()
                if text_align not in {"left", "center", "right"}:
                    text_align = "left"

                text_vertical_align = str(raw_layer.get("text_vertical_align") or "top").strip().lower()
                if text_vertical_align not in {"top", "middle", "bottom"}:
                    text_vertical_align = "top"

                font_weight = str(raw_layer.get("font_weight") or "normal").strip().lower()
                if font_weight not in {"normal", "bold"}:
                    font_weight = "normal"

                normalized_layer = {
                    "id": str(raw_layer.get("id") or f"layer-{layer_index + 1}"),
                    "name": str(raw_layer.get("name") or f"Layer {layer_index + 1}"),
                    "type": layer_type,
                    "enabled": bool(raw_layer.get("enabled", True)),
                    "z_position": z_position,
                    "text": str(raw_layer.get("text") or ""),
                    "image_url": str(raw_layer.get("image_url") or ""),
                    "color": str(raw_layer.get("color") or ("#1F232B" if layer_type == "block" else "#FFFFFF")),
                    "font_weight": font_weight,
                    "text_align": text_align,
                    "text_vertical_align": text_vertical_align,
                    "x": _safe_int(raw_layer.get("x"), 20, 0, 5000),
                    "y": _safe_int(raw_layer.get("y"), 20, 0, 5000),
                    "width": _safe_int(raw_layer.get("width"), 260, 1, 5000),
                    "height": _safe_int(raw_layer.get("height"), 80, 1, 5000),
                    "font_size": _safe_int(raw_layer.get("font_size"), 22, 8, 160),
                    "opacity": _safe_int(raw_layer.get("opacity"), 100, 0, 100),
                    "radius": _safe_int(raw_layer.get("radius"), 18, 0, 3000),
                }

                if layer_type == "avatar":
                    normalized_layer["border_width"] = _safe_int(raw_layer.get("border_width"), 3, 0, 64)
                    normalized_layer["border_color"] = str(raw_layer.get("border_color") or "#0A111B")

                if layer_type == "logo":
                    normalized_layer["background_color"] = str(raw_layer.get("background_color") or "#0B1420")
                    normalized_layer["background_opacity"] = _safe_int(raw_layer.get("background_opacity"), 72, 0, 100)
                    normalized_layer["border_width"] = _safe_int(raw_layer.get("border_width"), 1, 0, 32)
                    normalized_layer["border_color"] = str(raw_layer.get("border_color") or "#FFFFFF")
                    normalized_layer["border_opacity"] = _safe_int(raw_layer.get("border_opacity"), 22, 0, 100)

                normalized_layers.append(normalized_layer)

        images_by_id[image_id] = {
            "id": image_id,
            "name": str(entry.get("name") or f"Dynamic Image {index + 1}"),
            "width": image_width,
            "height": image_height,
            "background_color": background_color,
            "allow_transparent_background": allow_transparent_background,
            "layers": normalized_layers,
        }

    return images_by_id


def pick_weighted_welcome_message(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None

    weighted: list[tuple[dict, int]] = []
    total = 0
    for candidate in candidates:
        try:
            weight = max(1, int(candidate.get("weight") or 1))
        except (TypeError, ValueError):
            weight = 1
        weighted.append((candidate, weight))
        total += weight

    pick = random.uniform(0, total)
    for candidate, weight in weighted:
        pick -= weight
        if pick <= 0:
            return candidate

    return weighted[-1][0]


def resolve_welcome_dynamic_image_url(
    message_cfg: dict,
    dynamic_images: dict[str, dict],
    guild: discord.Guild,
    member: discord.Member | None,
) -> str:
    embed_image = render_onboarding_text(str(message_cfg.get("embed_image") or ""), guild, member)
    if embed_image.startswith("http"):
        return embed_image

    return ""


def message_uses_dynamic_attachment(message_cfg: dict) -> bool:
    return bool(str(message_cfg.get("dynamic_image_id") or "").strip())


def resolve_welcome_message_mode(message_cfg: dict) -> str:
    mode = str(message_cfg.get("message_mode") or "").strip().lower()
    if mode in {"normal", "embed"}:
        return mode
    return "normal" if message_uses_dynamic_attachment(message_cfg) else "embed"


async def resolve_sendable_guild_channel(guild: discord.Guild, channel_id: int | None):
    if not channel_id:
        return None

    channel = guild.get_channel(channel_id)
    if channel is not None and hasattr(channel, "send"):
        return channel

    try:
        fetched = await guild.fetch_channel(channel_id)
        if fetched is not None and hasattr(fetched, "send"):
            return fetched
    except Exception as channel_err:
        print(f"[Onboarding] Could not resolve channel {channel_id} in guild {guild.id}: {channel_err}")

    return None


async def resolve_member_from_payload(
    guild: discord.Guild,
    payload: dict,
    *,
    key: str,
) -> discord.Member | None:
    user_id = _as_int(payload.get(key))
    if not user_id:
        return None

    member = guild.get_member(user_id)
    if member is not None:
        return member

    try:
        fetched_member = await guild.fetch_member(user_id)
        if isinstance(fetched_member, discord.Member):
            return fetched_member
    except Exception:
        return None

    return None


def coerce_payload_str_list(payload: dict, key: str) -> list[str]:
    raw = payload.get(key, [])
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if not isinstance(raw, list):
        return []

    out: list[str] = []
    for entry in raw:
        text = str(entry or "").strip()
        if text:
            out.append(text)
    return out


async def run_welcome_simulation_trigger(guild: discord.Guild, cfg: dict, payload: dict) -> int:
    group_ids = coerce_payload_str_list(payload, "group_ids")
    message_ids = coerce_payload_str_list(payload, "message_ids")
    simulate_member = await resolve_member_from_payload(guild, payload, key="simulate_user_id")
    if simulate_member is None:
        simulate_member = await resolve_member_from_payload(guild, payload, key="user_id")

    if simulate_member is None:
        owner_member = guild.get_member(guild.owner_id)
        if owner_member is None:
            try:
                fetched_owner = await guild.fetch_member(guild.owner_id)
                if isinstance(fetched_owner, discord.Member):
                    owner_member = fetched_owner
            except Exception:
                owner_member = None
        simulate_member = owner_member

    return await send_onboarding_welcome_simulation(
        guild,
        cfg,
        group_ids,
        message_ids,
        simulate_member,
    )


def _color_to_rgba(color_value: str, opacity: int, default: tuple[int, int, int] = (255, 255, 255)) -> tuple[int, int, int, int]:
    raw = str(color_value or "").strip()
    rgb = default
    if raw.startswith("#"):
        hex_part = raw[1:]
        if len(hex_part) == 3:
            hex_part = "".join(ch * 2 for ch in hex_part)
        if len(hex_part) == 6:
            try:
                rgb = (
                    int(hex_part[0:2], 16),
                    int(hex_part[2:4], 16),
                    int(hex_part[4:6], 16),
                )
            except ValueError:
                rgb = default
    alpha = max(0, min(255, int(255 * max(0, min(100, opacity)) / 100)))
    return (rgb[0], rgb[1], rgb[2], alpha)


def _safe_int(value, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def _measure_text(draw_obj, text: str, font_obj) -> tuple[int, int]:
    try:
        if hasattr(draw_obj, "textbbox"):
            bbox = draw_obj.textbbox((0, 0), text, font=font_obj)
            return max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
    except Exception:
        pass

    try:
        if hasattr(draw_obj, "textsize"):
            width, height = draw_obj.textsize(text, font=font_obj)
            return max(1, int(width)), max(1, int(height))
    except Exception:
        pass

    try:
        if font_obj is not None and hasattr(font_obj, "getbbox"):
            bbox = font_obj.getbbox(text)
            return max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
    except Exception:
        pass

    try:
        if font_obj is not None and hasattr(font_obj, "getsize"):
            width, height = font_obj.getsize(text)
            return max(1, int(width)), max(1, int(height))
    except Exception:
        pass

    return max(1, len(text) * 6), 14


def _measure_multiline_text(draw_obj, text: str, font_obj, spacing: int, align: str) -> tuple[int, int]:
    try:
        if hasattr(draw_obj, "multiline_textbbox"):
            bbox = draw_obj.multiline_textbbox((0, 0), text, font=font_obj, spacing=spacing, align=align)
            return max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
    except Exception:
        pass

    try:
        if hasattr(draw_obj, "multiline_textsize"):
            width, height = draw_obj.multiline_textsize(text, font=font_obj, spacing=spacing)
            return max(1, int(width)), max(1, int(height))
    except Exception:
        pass

    lines = text.splitlines() or [text]
    max_width = 1
    total_height = 0
    for idx, line in enumerate(lines):
        line_width, line_height = _measure_text(draw_obj, line, font_obj)
        max_width = max(max_width, line_width)
        total_height += line_height
        if idx < len(lines) - 1:
            total_height += max(0, int(spacing))

    return max_width, max(1, total_height)


def _draw_rounded_rectangle(draw_obj, box: tuple[int, int, int, int], radius: int, fill):
    if hasattr(draw_obj, "rounded_rectangle"):
        draw_obj.rounded_rectangle(box, radius=max(0, int(radius)), fill=fill)
        return
    draw_obj.rectangle(box, fill=fill)


def _draw_rounded_outline(draw_obj, box: tuple[int, int, int, int], radius: int, outline, width: int):
    stroke = max(1, int(width))
    if hasattr(draw_obj, "rounded_rectangle"):
        draw_obj.rounded_rectangle(box, radius=max(0, int(radius)), outline=outline, width=stroke)
        return
    draw_obj.rectangle(box, outline=outline, width=stroke)


def _safe_alpha_composite(base_image, overlay_image, x: int, y: int):
    try:
        base_image.alpha_composite(overlay_image, (x, y))
        return
    except Exception:
        pass

    base_image.paste(overlay_image, (x, y), overlay_image)


def _load_dynamic_font(size: int, bold: bool = False):
    if ImageFont is None:
        return None

    safe_size = max(8, min(640, int(size)))
    candidates: list[str] = []

    if bold:
        candidates.extend([
            "segoeuib.ttf",
            "arialbd.ttf",
            "DejaVuSans-Bold.ttf",
            "segoeui.ttf",
            "arial.ttf",
            "DejaVuSans.ttf",
        ])
    else:
        candidates.extend(["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"])

    windir = os.getenv("WINDIR", "C:\\Windows")
    windows_fonts_dir = os.path.join(windir, "Fonts")
    if bold:
        candidates.extend([
            os.path.join(windows_fonts_dir, "segoeuib.ttf"),
            os.path.join(windows_fonts_dir, "arialbd.ttf"),
            os.path.join(windows_fonts_dir, "segoeui.ttf"),
            os.path.join(windows_fonts_dir, "arial.ttf"),
        ])
    else:
        candidates.extend([
            os.path.join(windows_fonts_dir, "segoeui.ttf"),
            os.path.join(windows_fonts_dir, "arial.ttf"),
        ])

    linux_font_dirs = [
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/truetype/liberation",
        "/usr/share/fonts/truetype/noto",
    ]
    if bold:
        linux_names = ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "NotoSans-Bold.ttf", "DejaVuSans.ttf"]
    else:
        linux_names = ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "NotoSans-Regular.ttf"]
    for base_dir in linux_font_dirs:
        for font_name in linux_names:
            candidates.append(os.path.join(base_dir, font_name))

    mac_font_dirs = [
        "/System/Library/Fonts",
        "/System/Library/Fonts/Supplemental",
        "/Library/Fonts",
    ]
    if bold:
        mac_names = ["Arial Bold.ttf", "Arial.ttf", "Helvetica.ttc"]
    else:
        mac_names = ["Arial.ttf", "Helvetica.ttc"]
    for base_dir in mac_font_dirs:
        for font_name in mac_names:
            candidates.append(os.path.join(base_dir, font_name))

    pil_dir = os.path.dirname(getattr(ImageFont, "__file__", "") or "")
    if pil_dir:
        pil_font_dirs = [
            os.path.join(pil_dir, "fonts"),
            os.path.join(pil_dir, "Fonts"),
        ]
        if bold:
            pil_font_names = ["DejaVuSans-Bold.ttf", "DejaVuSans.ttf"]
        else:
            pil_font_names = ["DejaVuSans.ttf"]
        for base_dir in pil_font_dirs:
            for font_name in pil_font_names:
                candidates.append(os.path.join(base_dir, font_name))

    deduped_candidates: list[str] = []
    for entry in candidates:
        path = str(entry or "").strip()
        if path and path not in deduped_candidates:
            deduped_candidates.append(path)

    for font_name in deduped_candidates:
        try:
            return ImageFont.truetype(font_name, safe_size)
        except Exception:
            continue

    try:
        return ImageFont.load_default(size=safe_size)
    except TypeError:
        pass
    return ImageFont.load_default()


def _resample_lanczos():
    if Image is None:
        return None
    if hasattr(Image, "Resampling"):
        return Image.Resampling.LANCZOS
    return Image.LANCZOS


def _apply_rounded_corners(image_obj, radius: int):
    if Image is None or ImageDraw is None:
        return image_obj

    width, height = image_obj.size
    clamped_radius = max(0, min(radius, min(width, height) // 2))
    if clamped_radius <= 0:
        return image_obj

    # Build the mask at higher resolution, then downsample for smoother edge antialiasing.
    scale = 4
    large_size = (max(1, width * scale), max(1, height * scale))
    mask = Image.new("L", large_size, 0)
    mask_draw = ImageDraw.Draw(mask)
    if hasattr(mask_draw, "rounded_rectangle"):
        mask_draw.rounded_rectangle((0, 0, large_size[0], large_size[1]), radius=clamped_radius * scale, fill=255)
    else:
        mask_draw.rectangle((0, 0, large_size[0], large_size[1]), fill=255)

    if hasattr(Image, "Resampling"):
        downsample = Image.Resampling.LANCZOS
    else:
        downsample = Image.LANCZOS
    mask = mask.resize((width, height), downsample)

    rounded = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    rounded.paste(image_obj, (0, 0), mask)
    return rounded


def _apply_image_opacity(image_obj, opacity: int):
    clamped = max(0, min(100, opacity))
    if clamped >= 100:
        return image_obj

    alpha = image_obj.getchannel("A")
    alpha = alpha.point(lambda px: int(px * clamped / 100))
    image_obj.putalpha(alpha)
    return image_obj


async def _fetch_image_bytes(url: str) -> bytes | None:
    if not str(url or "").startswith("http"):
        return None

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, ssl=_SSL_CTX) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                return data if data else None
    except Exception:
        return None


async def render_welcome_dynamic_image_bytes(
    dynamic_cfg: dict,
    guild: discord.Guild,
    member: discord.Member | None,
) -> bytes | None:
    global _DYNAMIC_IMAGE_RUNTIME_WARNING_EMITTED

    if not isinstance(dynamic_cfg, dict):
        return None
    if Image is None or ImageDraw is None:
        if not _DYNAMIC_IMAGE_RUNTIME_WARNING_EMITTED:
            print("[Onboarding] Dynamic image rendering disabled: Pillow is unavailable in this runtime.")
            _DYNAMIC_IMAGE_RUNTIME_WARNING_EMITTED = True
        return None

    width = _safe_int(dynamic_cfg.get("width"), 500, 128, 4000)
    height = _safe_int(dynamic_cfg.get("height"), 350, 128, 4000)
    quality_scale = _safe_int(dynamic_cfg.get("render_quality_scale"), 2, 1, 4)

    render_width = width * quality_scale
    render_height = height * quality_scale
    
    # Keep the canvas transparent so only configured layers are visible.
    background = (0, 0, 0, 0)

    canvas = Image.new("RGBA", (render_width, render_height), background)
    draw = ImageDraw.Draw(canvas, "RGBA")
    resample = _resample_lanczos()

    layers = dynamic_cfg.get("layers", [])
    if not isinstance(layers, list):
        layers = []

    def _normalized_z_position(layer_obj: dict) -> str:
        raw_value = str(layer_obj.get("z_position") or "").strip().lower()
        if raw_value in {"back", "front"}:
            return raw_value

        # Older configs can miss z_position; treat card/block layers as back by default.
        layer_type = str(layer_obj.get("type") or "").strip().lower()
        return "back" if layer_type == "block" else "front"

    sorted_layers: list[tuple[int, dict]] = []
    for layer_index, layer_obj in enumerate(layers):
        if not isinstance(layer_obj, dict):
            continue
        sorted_layers.append((layer_index, layer_obj))

    sorted_layers.sort(key=lambda pair: (0 if _normalized_z_position(pair[1]) == "back" else 1, pair[0]))

    for _, layer in sorted_layers:
        if not bool(layer.get("enabled", True)):
            continue

        try:
            layer_type = str(layer.get("type") or "text").strip().lower()
            logical_x = _safe_int(layer.get("x"), 20, 0, width)
            logical_y = _safe_int(layer.get("y"), 20, 0, height)
            logical_layer_width = _safe_int(layer.get("width"), 160, 1, width)
            logical_layer_height = _safe_int(layer.get("height"), 80, 1, height)
            opacity = _safe_int(layer.get("opacity"), 100, 0, 100)
            radius = _safe_int(layer.get("radius"), 0, 0, 3000) * quality_scale
            color = str(layer.get("color") or "#FFFFFF")

            max_w = max(1, width - logical_x)
            max_h = max(1, height - logical_y)
            logical_layer_width = min(logical_layer_width, max_w)
            logical_layer_height = min(logical_layer_height, max_h)

            x = logical_x * quality_scale
            y = logical_y * quality_scale
            layer_width = logical_layer_width * quality_scale
            layer_height = logical_layer_height * quality_scale

            if layer_type == "block":
                _draw_rounded_rectangle(
                    draw,
                    (x, y, x + layer_width, y + layer_height),
                    max(0, min(radius, min(layer_width, layer_height) // 2)),
                    _color_to_rgba(color, opacity, default=(31, 35, 43)),
                )
                continue

            if layer_type == "text":
                text = render_onboarding_text(str(layer.get("text") or ""), guild, member)
                if not text:
                    continue
                font_size = _safe_int(layer.get("font_size"), 22, 8, 160) * quality_scale
                font_weight = str(layer.get("font_weight") or "normal").strip().lower()
                is_bold = font_weight == "bold"
                text_align = str(layer.get("text_align") or "left").strip().lower()
                if text_align not in {"left", "center", "right"}:
                    text_align = "left"

                text_vertical_align = str(layer.get("text_vertical_align") or "top").strip().lower()
                if text_vertical_align not in {"top", "middle", "bottom"}:
                    text_vertical_align = "top"

                font = _load_dynamic_font(font_size, bold=is_bold)
                spacing = max(2, int(font_size * 0.16))
                text_w, text_h = _measure_multiline_text(draw, text, font, spacing, text_align)

                if text_align == "center":
                    draw_x = x + max(0, (layer_width - text_w) / 2)
                elif text_align == "right":
                    draw_x = x + max(0, layer_width - text_w)
                else:
                    draw_x = x

                if text_vertical_align == "middle":
                    draw_y = y + max(0, (layer_height - text_h) / 2)
                elif text_vertical_align == "bottom":
                    draw_y = y + max(0, layer_height - text_h)
                else:
                    draw_y = y

                draw.multiline_text(
                    (draw_x, draw_y),
                    text,
                    fill=_color_to_rgba(color, opacity),
                    font=font,
                    spacing=spacing,
                    align=text_align,
                )
                continue

            if layer_type in {"avatar", "logo"}:
                if layer_type == "logo":
                    logo_radius = max(0, min(radius, min(layer_width, layer_height) // 2))
                    logo_bg_color = str(layer.get("background_color") or "#0B1420")
                    logo_bg_opacity = _safe_int(layer.get("background_opacity"), 0, 0, 100)
                    logo_border_width = _safe_int(layer.get("border_width"), 0, 0, 32) * quality_scale
                    logo_border_color = str(layer.get("border_color") or "#FFFFFF")
                    logo_border_opacity = _safe_int(layer.get("border_opacity"), 0, 0, 100)

                    if logo_bg_opacity > 0:
                        _draw_rounded_rectangle(
                            draw,
                            (x, y, x + layer_width, y + layer_height),
                            logo_radius,
                            _color_to_rgba(logo_bg_color, logo_bg_opacity, default=(11, 20, 32)),
                        )

                    if logo_border_width > 0:
                        _draw_rounded_outline(
                            draw,
                            (x, y, x + layer_width - 1, y + layer_height - 1),
                            logo_radius,
                            _color_to_rgba(logo_border_color, logo_border_opacity, default=(255, 255, 255)),
                            logo_border_width,
                        )

                image_bytes = None
                if layer_type == "avatar" and member is not None:
                    target_avatar_size = max(128, min(4096, max(layer_width, layer_height) * 4))
                    requested_avatar_size = 64
                    while requested_avatar_size < target_avatar_size and requested_avatar_size < 4096:
                        requested_avatar_size *= 2
                    try:
                        avatar_url = member.display_avatar.replace(format="png", size=requested_avatar_size).url
                    except Exception:
                        avatar_url = member.display_avatar.url
                    image_bytes = await _fetch_image_bytes(str(avatar_url or ""))
                elif layer_type == "logo":
                    logo_url = render_onboarding_text(str(layer.get("image_url") or ""), guild, member)
                    image_bytes = await _fetch_image_bytes(logo_url)

                layer_image = None
                if image_bytes:
                    try:
                        layer_image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
                    except Exception:
                        layer_image = None

                if layer_image is None and layer_type == "avatar":
                    # Fallback avatar chip if fetch fails.
                    layer_image = Image.new("RGBA", (layer_width, layer_height), (0, 0, 0, 0))
                    fallback_draw = ImageDraw.Draw(layer_image, "RGBA")
                    fallback_draw.ellipse((0, 0, layer_width, layer_height), fill=(92, 114, 255, 255))
                    initial = "U"
                    if member is not None:
                        initial = (member.display_name or member.name or "U").strip()[:1].upper() or "U"
                    font = _load_dynamic_font(max(12, int(min(layer_width, layer_height) * 0.42)))
                    text_w, text_h = _measure_text(fallback_draw, initial, font)
                    fallback_draw.text(
                        ((layer_width - text_w) / 2, (layer_height - text_h) / 2 - 2),
                        initial,
                        fill=(255, 255, 255, 255),
                        font=font,
                    )

                if layer_image is None:
                    continue

                # Use cover-fit behavior for image layers to match dashboard object-cover preview.
                if ImageOps is not None:
                    try:
                        if resample is not None:
                            layer_image = ImageOps.fit(
                                layer_image,
                                (layer_width, layer_height),
                                method=resample,
                                centering=(0.5, 0.5),
                            )
                        else:
                            layer_image = ImageOps.fit(layer_image, (layer_width, layer_height), centering=(0.5, 0.5))
                    except TypeError:
                        if resample is not None:
                            layer_image = ImageOps.fit(
                                layer_image,
                                (layer_width, layer_height),
                                resample=resample,
                                centering=(0.5, 0.5),
                            )
                        else:
                            layer_image = ImageOps.fit(layer_image, (layer_width, layer_height), centering=(0.5, 0.5))
                else:
                    if resample is not None:
                        layer_image = layer_image.resize((layer_width, layer_height), resample)
                    else:
                        layer_image = layer_image.resize((layer_width, layer_height))

                if layer_type == "avatar":
                    radius = max(radius, min(layer_width, layer_height) // 2)

                layer_image = _apply_rounded_corners(layer_image, radius)
                layer_image = _apply_image_opacity(layer_image, opacity)
                _safe_alpha_composite(canvas, layer_image, x, y)

                if layer_type == "avatar":
                    avatar_border_width = _safe_int(layer.get("border_width"), 3, 0, 64) * quality_scale
                    if avatar_border_width > 0:
                        avatar_border_color = _color_to_rgba(
                            str(layer.get("border_color") or "#0A111B"),
                            100,
                            default=(10, 17, 27),
                        )
                        _draw_rounded_outline(
                            draw,
                            (x, y, x + layer_width - 1, y + layer_height - 1),
                            radius,
                            avatar_border_color,
                            avatar_border_width,
                        )
        except Exception as layer_err:
            layer_name = str(layer.get("name") or layer.get("id") or "layer")
            print(f"[Onboarding] Dynamic image layer render failed ({layer_name}): {layer_err}")
            continue

    try:
        if quality_scale > 1:
            if resample is not None:
                canvas = canvas.resize((width, height), resample)
            else:
                canvas = canvas.resize((width, height))

        output = io.BytesIO()
        canvas.save(output, format="PNG")
        return output.getvalue()
    except Exception as save_err:
        print(f"[Onboarding] Dynamic image encode failed: {save_err}")
        return None


async def resolve_welcome_dynamic_attachment_file(
    message_cfg: dict,
    dynamic_images: dict[str, dict],
    guild: discord.Guild,
    member: discord.Member | None,
    *,
    filename_prefix: str,
) -> discord.File | None:
    dynamic_image_id = str(message_cfg.get("dynamic_image_id") or "")
    if not dynamic_image_id:
        return None

    dynamic_cfg = dynamic_images.get(dynamic_image_id)
    if not isinstance(dynamic_cfg, dict):
        return None

    try:
        rendered = await render_welcome_dynamic_image_bytes(dynamic_cfg, guild, member)
    except Exception as render_err:
        print(f"[Onboarding] Dynamic image render failed ({dynamic_image_id}): {render_err}")
        return None

    if not rendered:
        return None

    filename = f"{filename_prefix}-{dynamic_image_id}.png"
    return discord.File(io.BytesIO(rendered), filename=filename)


def build_welcome_embed_from_message(
    message_cfg: dict,
    cfg: dict,
    guild: discord.Guild,
    member: discord.Member | None,
    dynamic_images: dict[str, dict],
) -> discord.Embed:
    embed = discord.Embed(
        title=render_onboarding_text(str(message_cfg.get("embed_title") or cfg.get("welcome_embed_title") or ""), guild, member),
        description=render_onboarding_text(str(message_cfg.get("embed_description") or cfg.get("welcome_embed_description") or ""), guild, member),
        color=parse_embed_color(message_cfg.get("embed_color") or cfg.get("welcome_embed_color"), 0x5865F2),
        timestamp=datetime.now(timezone.utc),
    )

    thumbnail_url = render_onboarding_text(str(message_cfg.get("embed_thumbnail") or ""), guild, member)
    if thumbnail_url.startswith("http"):
        embed.set_thumbnail(url=thumbnail_url)

    image_url = resolve_welcome_dynamic_image_url(message_cfg, dynamic_images, guild, member)
    if image_url.startswith("http"):
        embed.set_image(url=image_url)

    footer_text = render_onboarding_text(str(message_cfg.get("embed_footer") or cfg.get("welcome_embed_footer") or ""), guild, member)
    if footer_text:
        embed.set_footer(text=footer_text)

    return embed


async def send_onboarding_welcome_simulation(
    guild: discord.Guild,
    cfg: dict,
    group_ids: list[str] | None = None,
    message_ids: list[str] | None = None,
    simulate_member: discord.Member | None = None,
) -> int:
    groups = normalize_welcome_message_groups(cfg)
    messages = normalize_welcome_messages(cfg, groups)
    dynamic_images = normalize_welcome_dynamic_images(cfg)

    default_channel_id = _as_int(cfg.get("welcome_channel_id"))
    target_group_ids = {str(group_id).strip() for group_id in (group_ids or []) if str(group_id).strip()}
    target_message_ids = {str(message_id).strip() for message_id in (message_ids or []) if str(message_id).strip()}
    sent_count = 0

    for group in groups:
        group_id = str(group.get("id") or "")
        if target_group_ids:
            if group_id not in target_group_ids:
                continue
        elif not bool(group.get("enabled", True)):
            continue

        candidates = [
            message
            for message in messages
            if str(message.get("group_id") or "") == group_id and bool(message.get("enabled", True))
        ]
        if target_message_ids:
            selected = next(
                (
                    message
                    for message in candidates
                    if str(message.get("id") or "") in target_message_ids
                ),
                None,
            )
        else:
            selected = pick_weighted_welcome_message(candidates)
        if not selected:
            continue

        channel_id = _as_int(group.get("channel_id")) or default_channel_id
        if not channel_id:
            continue

        channel = await resolve_sendable_guild_channel(guild, channel_id)
        if channel is None:
            continue

        content = render_onboarding_text(
            str(selected.get("content") or cfg.get("welcome_content") or ""),
            guild,
            simulate_member,
        ).strip()
        delivery_mode = resolve_welcome_message_mode(selected)

        if message_uses_dynamic_attachment(selected):
            image_file = await resolve_welcome_dynamic_attachment_file(
                selected,
                dynamic_images,
                guild,
                simulate_member,
                filename_prefix="welcome-sim",
            )
            payload_lines = ["🧪 **Welcome Simulation**"]
            if content:
                payload_lines.append(content)

            payload_content = "\n".join(payload_lines).strip() or "🧪 **Welcome Simulation**"

            if image_file is None:
                dynamic_image_id = str(selected.get("dynamic_image_id") or "")
                print(f"[Onboarding] Dynamic image simulation render returned no attachment ({dynamic_image_id}).")
                await channel.send(content=payload_content)
            else:
                await channel.send(content=payload_content, file=image_file)
        elif delivery_mode == "normal":
            image_url = resolve_welcome_dynamic_image_url(selected, dynamic_images, guild, simulate_member)
            payload_lines = ["🧪 **Welcome Simulation**"]
            if content:
                payload_lines.append(content)
            if image_url:
                payload_lines.append(image_url)

            payload_content = "\n".join(payload_lines).strip()
            if not payload_content:
                continue

            await channel.send(content=payload_content)
        else:
            embed = build_welcome_embed_from_message(selected, cfg, guild, simulate_member, dynamic_images)
            simulation_content = f"🧪 **Welcome Simulation**\n{content}".strip()
            await channel.send(content=simulation_content or None, embed=embed)

        sent_count += 1

    return sent_count


async def assign_onboarding_roles(member: discord.Member, cfg: dict, include_verified_role: bool):
    role_ids = []
    if include_verified_role:
        verified_role_id = _as_int(cfg.get("verified_role_id"))
        if verified_role_id:
            role_ids.append(verified_role_id)

    for role_id in cfg.get("auto_role_ids", []):
        rid = _as_int(role_id)
        if rid:
            role_ids.append(rid)

    unique_ids = []
    for rid in role_ids:
        if rid not in unique_ids:
            unique_ids.append(rid)

    roles_to_add = []
    for rid in unique_ids:
        role = member.guild.get_role(rid)
        if role and role not in member.roles:
            roles_to_add.append(role)

    if roles_to_add:
        await member.add_roles(*roles_to_add, reason="Onboarding role assignment")


async def send_onboarding_welcome(member: discord.Member, cfg: dict):
    if not cfg.get("welcome_enabled", True):
        return

    groups = normalize_welcome_message_groups(cfg)
    messages = normalize_welcome_messages(cfg, groups)
    dynamic_images = normalize_welcome_dynamic_images(cfg)

    default_channel_id = _as_int(cfg.get("welcome_channel_id"))
    delivered_count = 0

    for group in groups:
        if not bool(group.get("enabled", True)):
            continue

        group_id = str(group.get("id") or "")
        candidates = [
            message
            for message in messages
            if str(message.get("group_id") or "") == group_id and bool(message.get("enabled", True))
        ]

        selected = pick_weighted_welcome_message(candidates)
        if not selected:
            continue

        channel_id = _as_int(group.get("channel_id")) or default_channel_id
        if not channel_id:
            continue

        channel = await resolve_sendable_guild_channel(member.guild, channel_id)
        if channel is None:
            continue

        content = render_onboarding_text(str(selected.get("content") or cfg.get("welcome_content") or ""), member.guild, member).strip()
        delivery_mode = resolve_welcome_message_mode(selected)
        if message_uses_dynamic_attachment(selected):
            image_file = await resolve_welcome_dynamic_attachment_file(
                selected,
                dynamic_images,
                member.guild,
                member,
                filename_prefix=f"welcome-{member.id}",
            )
            payload_content = content or None

            if image_file is None:
                dynamic_image_id = str(selected.get("dynamic_image_id") or "")
                print(f"[Onboarding] Dynamic image render returned no attachment ({dynamic_image_id}) for member {member.id}.")
                if payload_content is None:
                    continue
                await channel.send(content=payload_content)
            else:
                await channel.send(content=payload_content, file=image_file)
        elif delivery_mode == "normal":
            image_url = resolve_welcome_dynamic_image_url(selected, dynamic_images, member.guild, member)
            payload_lines = []
            if content:
                payload_lines.append(content)
            if image_url:
                payload_lines.append(image_url)

            payload_content = "\n".join(payload_lines).strip()
            if not payload_content:
                continue

            await channel.send(content=payload_content)
        else:
            embed = build_welcome_embed_from_message(selected, cfg, member.guild, member, dynamic_images)
            await channel.send(content=content or None, embed=embed)
        delivered_count += 1

    if delivered_count > 0:
        return

    raw_builder_messages = cfg.get("welcome_messages")
    if isinstance(raw_builder_messages, list) and len(raw_builder_messages) > 0:
        # If builder messages exist, do not silently fall back to legacy template/embed.
        # This avoids sending unexpected non-builder welcomes when dynamic rendering fails.
        return

    # Backward-compatible fallback in case builder groups/messages are malformed.
    if not default_channel_id:
        return
    fallback_channel = await resolve_sendable_guild_channel(member.guild, default_channel_id)
    if fallback_channel is None:
        return

    content = render_onboarding_text(cfg.get("welcome_content"), member.guild, member).strip()
    embed = build_welcome_embed(cfg, member.guild, member)
    await fallback_channel.send(content=content or None, embed=embed)


async def run_join_guard(member: discord.Member, cfg: dict) -> bool:
    if not cfg.get("join_guard_enabled", False):
        return True

    reasons = []
    min_days = max(0, int(cfg.get("min_account_age_days") or 0))
    if min_days > 0:
        age = datetime.now(timezone.utc) - member.created_at
        if age < timedelta(days=min_days):
            reasons.append(
                f"Account is too new ({age.days}d old, minimum required is {min_days}d)."
            )

    if cfg.get("block_default_avatar", False) and member.avatar is None:
        reasons.append("Account has no custom avatar.")

    if not reasons:
        return True

    reason_text = "Join Guard: " + " | ".join(reasons)

    # Always try to DM blocked members with the exact reason before enforcement.
    try:
        try:
            embed = discord.Embed(
                title=f"🛑 Blocked from {member.guild.name}",
                description="You were automatically removed from the server by the **Join Guard** system.",
                color=0xED4245
            )
            embed.add_field(name="Reason(s)", value="\n".join(f"• {r}" for r in reasons), inline=False)
            await member.send(embed=embed)
        except Exception:
            # Fallback to plain text if embed fails
            await member.send(
                f"You were blocked from **{member.guild.name}** by Join Guard.\n"
                f"Reason: {reason_text}"
            )
    except Exception:
        pass

    action = str(cfg.get("join_guard_action") or "kick").lower()
    try:
        if action == "ban":
            await member.guild.ban(member, reason=reason_text, delete_message_days=0)
        else:
            await member.guild.kick(member, reason=reason_text)
    except Exception as e:
        print(f"[Onboarding] Failed to enforce join guard: {e}")

    log_channel_id = _as_int(cfg.get("join_guard_log_channel_id"))
    if log_channel_id:
        log_channel = member.guild.get_channel(log_channel_id)
        if log_channel and hasattr(log_channel, "send"):
            try:
                embed = discord.Embed(
                    title="🛡️ Join Guard Triggered",
                    description=f"{member.mention} was removed by join guard.",
                    color=0xED4245,
                    timestamp=datetime.now(timezone.utc),
                )
                embed.add_field(name="Action", value=action.upper(), inline=True)
                embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)
                embed.add_field(name="Reason", value=reason_text[:1024], inline=False)
                await log_channel.send(embed=embed)
            except Exception:
                pass

    return False


async def handle_new_member_onboarding(member: discord.Member):
    if member.bot:
        return

    cfg = get_onboarding_config(member.guild.id)
    onboarding_active = bool(
        cfg.get("enabled")
        or cfg.get("welcome_enabled")
        or cfg.get("join_guard_enabled")
    )
    if not onboarding_active:
        return

    passed_join_guard = await run_join_guard(member, cfg)
    if not passed_join_guard:
        return

    try:
        await assign_onboarding_roles(member, cfg, include_verified_role=True)
    except Exception as e:
        print(f"[Onboarding] Auto role assignment failed: {e}")

    if cfg.get("send_welcome_on_join", False):
        try:
            await send_onboarding_welcome(member, cfg)
        except Exception as e:
            print(f"[Onboarding] Failed to send welcome message: {e}")


def register(bot):
    bot.add_listener(handle_new_member_onboarding, 'on_member_join')

