"""
modules/social_monitor.py
Social monitor system.
"""

from __future__ import annotations
import re, asyncio, ssl, certifi
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import aiohttp
import discord
from discord.ext import tasks
from modules.utils import _as_int, load_json, save_json

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

SOCIAL_FILE = "social_monitors"
SOCIAL_HEALTH_FILE = "social_monitor_health"
_YT_CHANNEL_ID_RE = re.compile(r"UC[a-zA-Z0-9_-]{22}")
_VIDEO_ID_RE = re.compile(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"')

bot = None


def _sanitize_social_error(error_value: object, max_len: int = 320) -> str:
    message = str(error_value or "").strip()
    if not message:
        return "Unknown social monitor error."

    compact = re.sub(r"\s+", " ", message)
    if (
        "$Sreact.fragment" in compact
        or "ClientPageRoot" in compact
        or "MetadataBoundary" in compact
    ):
        return "Received a non-feed payload while checking this source. Verify Source points to a valid URL."

    if len(compact) > max_len:
        return f"{compact[:max_len - 3]}..."
    return compact


def _extract_youtube_channel_id_from_html(html: str) -> str | None:
    if not html:
        return None

    patterns = [
        r'"channelId"\s*:\s*"(UC[a-zA-Z0-9_-]{22})"',
        r'<meta\s+itemprop="channelId"\s+content="(UC[a-zA-Z0-9_-]{22})"',
        r'youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})',
        r'youtube\.com\\/channel\\/(UC[a-zA-Z0-9_-]{22})',
    ]

    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)

    return None


def load_social_monitors() -> dict:
    data = load_json(SOCIAL_FILE, {})
    if not isinstance(data, dict):
        return {}

    normalized = {}
    for guild_id, cfg in data.items():
        cfgs = [cfg] if isinstance(cfg, dict) else cfg if isinstance(cfg, list) else []
        normalized_cfgs = []

        for entry in cfgs:
            if not isinstance(entry, dict):
                continue

            normalized_cfgs.append({
                "name": str(entry.get("name") or "").strip() or None,
                "platform": str(entry.get("platform") or "rss").strip().lower(),
                "source": str(entry.get("source") or "").strip(),
                "channel_id": _as_int(entry.get("channel_id")),
                "role_id": _as_int(entry.get("role_id")),
                "enabled": bool(entry.get("enabled", True)),
                "last_entry_id": str(entry.get("last_entry_id") or "").strip() or None,
                "last_checked_at": str(entry.get("last_checked_at") or "").strip() or None,
                "last_posted_at": str(entry.get("last_posted_at") or "").strip() or None,
                "last_error": _sanitize_social_error(entry.get("last_error")) if entry.get("last_error") else None,
            })

        normalized[str(guild_id)] = normalized_cfgs

    return normalized


def save_social_monitors(data: dict):
    save_json(SOCIAL_FILE, data)


def load_social_health() -> dict:
    data = load_json(SOCIAL_HEALTH_FILE, {})
    return data if isinstance(data, dict) else {}


def save_social_health(data: dict):
    save_json(SOCIAL_HEALTH_FILE, data)


def set_social_health(**updates):
    health = load_social_health()
    health.update(updates)
    save_social_health(health)


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _entry_text(entry: ET.Element, names: set[str]) -> str | None:
    for node in entry.iter():
        if _strip_ns(node.tag) in names and node.text and node.text.strip():
            return node.text.strip()
    return None


def _entry_author(entry: ET.Element) -> str | None:
    for node in entry.iter():
        local = _strip_ns(node.tag)
        if local == "creator" and node.text and node.text.strip():
            return node.text.strip()
        if local == "author":
            if node.text and node.text.strip():
                return node.text.strip()
            for child in node.iter():
                if _strip_ns(child.tag) == "name" and child.text and child.text.strip():
                    return child.text.strip()
    return None


def _entry_link(entry: ET.Element) -> str | None:
    for node in entry.iter():
        local = _strip_ns(node.tag)
        if local == "link":
            href = (node.attrib.get("href") or "").strip()
            if href:
                return href
            if node.text and node.text.strip().startswith("http"):
                return node.text.strip()
        if local in {"url", "origlink"} and node.text and node.text.strip().startswith("http"):
            return node.text.strip()
    return None


def _entry_thumbnail(entry: ET.Element) -> str | None:
    for node in entry.iter():
        local = _strip_ns(node.tag)
        if local == "thumbnail":
            thumb = (node.attrib.get("url") or node.text or "").strip()
            if thumb.startswith("http"):
                return thumb
        if local == "content":
            url = (node.attrib.get("url") or "").strip()
            media_type = (node.attrib.get("type") or "").lower()
            if url.startswith("http") and media_type.startswith("image"):
                return url
    return None


def _extract_youtube_video_url(entry_id: str | None) -> str | None:
    if not entry_id:
        return None
    raw = str(entry_id).strip()
    if not raw:
        return None
    if raw.startswith("http"):
        return raw
    candidate = raw.rsplit(":", 1)[-1]
    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", candidate):
        return f"https://www.youtube.com/watch?v={candidate}"
    return None


def _parse_feed_time(value: str | None) -> datetime | None:
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except Exception:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _resolve_youtube_channel_id(source: str, session: aiohttp.ClientSession) -> str | None:
    src = (source or "").strip()
    if not src:
        return None

    direct_match = _YT_CHANNEL_ID_RE.search(src)
    if direct_match:
        return direct_match.group(0)

    if not src.startswith("http"):
        if src.startswith("@"):  # handle
            src = f"https://www.youtube.com/{src}"
        elif src.startswith("youtube.com/"):
            src = f"https://{src}"
        elif src.startswith("www.youtube.com/"):
            src = f"https://{src}"
        elif "/" not in src and " " not in src:
            src = f"https://www.youtube.com/@{src.lstrip('@')}"
        else:
            return None

    if "youtube.com" not in src and "youtu.be" not in src:
        return None

    try:
        async with session.get(
            src,
            ssl=_SSL_CTX,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status >= 400:
                return None
            html = await resp.text()
    except Exception:
        return None

    return _extract_youtube_channel_id_from_html(html)


def _normalize_youtube_source_url(source: str) -> str | None:
    src = (source or "").strip()
    if not src:
        return None

    if src.startswith("http://") or src.startswith("https://"):
        return src
    if src.startswith("youtube.com/") or src.startswith("www.youtube.com/"):
        return f"https://{src}"
    if src.startswith("@"):
        return f"https://www.youtube.com/{src}"
    if _YT_CHANNEL_ID_RE.fullmatch(src):
        return f"https://www.youtube.com/channel/{src}"
    if "/" not in src and " " not in src:
        return f"https://www.youtube.com/@{src.lstrip('@')}"
    return None


def _build_youtube_tracking_url(source: str, channel_id: str | None) -> str | None:
    if channel_id:
        return f"https://www.youtube.com/channel/{channel_id}/videos"

    normalized = _normalize_youtube_source_url(source)
    if not normalized:
        return None

    lowered = normalized.lower().rstrip("/")
    if lowered.endswith("/videos"):
        return normalized
    return f"{normalized.rstrip('/')}/videos"


async def _scrape_youtube_channel_latest_video_id(channel_id: str, session: aiohttp.ClientSession) -> str | None:
    """
    Scrape the YouTube channel /videos page to find the latest video ID.
    The channel page tends to update faster than the RSS feed.
    Only returns a video ID if it can be found reliably in the page content.
    """
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    try:
        async with session.get(
            url,
            ssl=_SSL_CTX,
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        ) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
    except Exception:
        return None

    # Only look in the initial data section (before player/related video data)
    # to avoid matching recommended or embedded video IDs.
    cutoff = html.find("ytInitialPlayerResponse")
    search_area = html[:cutoff] if cutoff > 0 else html

    # Collect all video IDs found in the main page content
    ids_found = _VIDEO_ID_RE.findall(search_area)
    return ids_found[0] if ids_found else None


async def _fetch_youtube_video_oembed(video_id: str, fallback_author: str, session: aiohttp.ClientSession) -> dict:
    """Fetch video metadata via YouTube's oEmbed endpoint."""
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    oembed_url = f"https://www.youtube.com/oembed?url={video_url}&format=json"
    title = "New YouTube upload"
    author = fallback_author

    try:
        async with session.get(oembed_url, ssl=_SSL_CTX, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                title = data.get("title") or title
                author = data.get("author_name") or author
    except Exception:
        pass

    return {
        "id": video_id,
        "title": title,
        "author": author,
        "link": video_url,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "published_raw": None,
        "published_at": datetime.now(timezone.utc),
    }


async def fetch_youtube_latest_entry(
    source: str,
    session: aiohttp.ClientSession,
    previous_id: str | None = None,
) -> tuple[str, dict]:
    """Fetch the latest video from a YouTube channel using the public Atom RSS feed,
    with an optional fast-path scrape of the channel page to detect new uploads
    faster (YouTube's RSS can lag 15-30 min behind actual uploads).

    previous_id: the last known posted video ID. Used to validate the scraped
    result — we never trust a scraped ID that was already posted."""
    src = (source or "").strip()
    if not src:
        raise RuntimeError("Missing YouTube source URL or channel id.")

    channel_id = await _resolve_youtube_channel_id(src, session)
    if not channel_id:
        raise RuntimeError(
            "Could not resolve a YouTube Channel ID from Source. "
            "Use a channel URL, @handle, or UC... channel ID."
        )

    tracking_url = f"https://www.youtube.com/channel/{channel_id}/videos"
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

    # ── 1. Fetch RSS (always attempted, but optional if scraping works) ──
    rss_entry = None
    rss_video_ids: set[str] = set()
    rss_error = None

    try:
        async with session.get(feed_url, ssl=_SSL_CTX, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                xml_text = await resp.text()
                try:
                    root = ET.fromstring(xml_text)
                    entries = [node for node in root.iter() if _strip_ns(node.tag) == "entry"]
                    if entries:
                        for e in entries:
                            vid = _entry_text(e, {"videoid"})
                            if not vid:
                                eid = _entry_text(e, {"id"})
                                vid = eid.rsplit(":", 1)[-1] if eid else None
                            if vid:
                                rss_video_ids.add(vid)

                        entry = entries[0]
                        video_id_node = _entry_text(entry, {"videoid"})
                        entry_id_node = _entry_text(entry, {"id"})
                        title = _entry_text(entry, {"title"}) or "New YouTube upload"
                        author = _entry_author(entry)
                        published_raw = _entry_text(entry, {"published", "updated"})
                        published_at = _parse_feed_time(published_raw)

                        video_id = video_id_node
                        if not video_id and entry_id_node:
                            video_id = entry_id_node.rsplit(":", 1)[-1]

                        link = _entry_link(entry)
                        if not link and video_id:
                            link = f"https://www.youtube.com/watch?v={video_id}"

                        stable_id = video_id or link
                        thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else _entry_thumbnail(entry)

                        rss_entry = {
                            "id": stable_id,
                            "title": title,
                            "author": author or src,
                            "link": link,
                            "thumbnail": thumbnail,
                            "published_raw": published_raw,
                            "published_at": published_at,
                        }
                except ET.ParseError as e:
                    rss_error = f"YouTube RSS feed parse failed: {e}"
            else:
                rss_error = f"YouTube RSS feed request failed with status {resp.status}"
    except Exception as e:
        rss_error = f"YouTube RSS request exception: {e}"

    # ── 2. Fast-path: scrape channel page for a fresher video ID ─────────
    # We always attempt this, and it doubles as a fallback if RSS fails!
    try:
        scraped_id = await _scrape_youtube_channel_latest_video_id(channel_id, session)
        if scraped_id:
            if not rss_entry or scraped_id not in rss_video_ids:
                if scraped_id != (previous_id or ""):
                    # Genuinely new video — not in RSS yet, not already posted
                    author_fallback = rss_entry["author"] if rss_entry else src
                    scraped_entry = await _fetch_youtube_video_oembed(scraped_id, author_fallback, session)
                    return tracking_url, scraped_entry
                elif rss_entry:
                    # Scraped the video we already posted, RSS is still stale.
                    return tracking_url, {**rss_entry, "id": scraped_id}
                else:
                    # No RSS, and the scraped ID equals previous_id. Return it to stop loop.
                    author_fallback = src
                    scraped_entry = await _fetch_youtube_video_oembed(scraped_id, author_fallback, session)
                    return tracking_url, scraped_entry
    except Exception:
        pass  # Always fall through to RSS on any scrape/oEmbed error

    if rss_entry:
        return tracking_url, rss_entry

    if rss_error:
        raise RuntimeError(rss_error)
    raise RuntimeError("Both YouTube RSS feed and channel scraping failed to find videos.")





def resolve_social_source_url(
    platform: str,
    source: str,
) -> str | None:
    p = (platform or "rss").strip().lower()
    src = (source or "").strip()

    if not src:
        return None

    if p == "tiktok":
        if src.startswith("http://") or src.startswith("https://"):
            return src
        handle = src.lstrip("@").strip()
        if handle:
            return f"https://rsshub.app/tiktok/user/{handle}"
        return None

    if src.startswith("http://") or src.startswith("https://"):
        return src
    return None


async def fetch_source_latest_entry(source_url: str, session: aiohttp.ClientSession) -> dict | None:
    async with session.get(
        source_url,
        ssl=_SSL_CTX,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Source request failed with status {resp.status}")
        xml_text = await resp.text()

    probe = xml_text[:500].lower()
    if "<html" in probe and "<rss" not in probe and "<feed" not in probe:
        raise RuntimeError(
            "Source URL returned HTML instead of RSS/Atom XML. "
            "For YouTube, set Source to the channel URL/@handle/UC... ID."
        )

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise RuntimeError(f"Feed parse failed: {e}")

    entries = [node for node in root.iter() if _strip_ns(node.tag) in {"entry", "item"}]
    if not entries:
        return None

    entry = entries[0]
    entry_id = _entry_text(entry, {"id", "guid", "videoid"})
    link = _entry_link(entry) or _extract_youtube_video_url(entry_id)
    title = _entry_text(entry, {"title"}) or "New post"
    author = _entry_author(entry)
    published_raw = _entry_text(entry, {"published", "updated", "pubdate", "date"})
    published_at = _parse_feed_time(published_raw)
    thumbnail = _entry_thumbnail(entry)

    stable_id = (entry_id or link or title or "").strip()
    if not stable_id:
        return None

    return {
        "id": stable_id,
        "title": title,
        "author": author,
        "link": link,
        "thumbnail": thumbnail,
        "published_raw": published_raw,
        "published_at": published_at,
    }


def _social_platform_label(platform: str) -> str:
    p = (platform or "rss").lower()
    if p == "youtube":
        return "YouTube"
    if p == "tiktok":
        return "TikTok"
    return "RSS/Atom"


def _social_platform_color(platform: str) -> int:
    p = (platform or "rss").lower()
    if p == "youtube":
        return 0xFF0000
    if p == "tiktok":
        return 0x111111
    return 0x5865F2


def _is_http_url(value: str | None) -> bool:
    text = str(value or "").strip()
    return text.startswith("http://") or text.startswith("https://")


def _extract_tiktok_handle_from_url(url: str) -> str | None:
    src = str(url or "").strip()
    if not src:
        return None

    # RSSHub TikTok feed format: https://rsshub.app/tiktok/user/<handle>
    marker = "rsshub.app/tiktok/user/"
    lower_src = src.lower()
    idx = lower_src.find(marker)
    if idx != -1:
        tail = src[idx + len(marker):].strip().strip("/")
        handle = tail.split("/")[0].strip().lstrip("@")
        return handle or None

    # TikTok profile URL format: https://www.tiktok.com/@<handle>
    at_idx = src.find("@")
    if at_idx != -1:
        tail = src[at_idx + 1:].strip().strip("/")
        handle = tail.split("/")[0].strip().lstrip("@")
        return handle or None

    return None


def _derive_social_profile_url(monitor: dict, entry: dict) -> str | None:
    platform = str(monitor.get("platform") or "rss").strip().lower()
    source = str(monitor.get("source") or "").strip()
    entry_source_url = str(entry.get("source_url") or "").strip()

    if platform == "youtube":
        normalized = _normalize_youtube_source_url(source)
        if normalized:
            return normalized
        if _is_http_url(entry_source_url):
            return entry_source_url
        return None

    if platform == "tiktok":
        handle = _extract_tiktok_handle_from_url(source)
        if not handle:
            handle = _extract_tiktok_handle_from_url(entry_source_url)
        if not handle and source and not _is_http_url(source):
            candidate = source.lstrip("@").strip()
            if candidate and "/" not in candidate and " " not in candidate:
                handle = candidate
        if handle:
            return f"https://www.tiktok.com/@{handle}"
        if _is_http_url(source):
            return source
        if _is_http_url(entry_source_url):
            return entry_source_url
        return None

    # RSS/other sources
    if _is_http_url(source):
        return source
    if _is_http_url(entry_source_url):
        return entry_source_url
    return None


def build_social_action_view(monitor: dict, entry: dict) -> discord.ui.View | None:
    platform = str(monitor.get("platform") or "rss").strip().lower()
    profile_url = _derive_social_profile_url(monitor, entry)
    open_url = str(entry.get("link") or "").strip()

    if not _is_http_url(profile_url):
        profile_url = ""
    if not _is_http_url(open_url):
        open_url = ""

    if not profile_url and not open_url:
        return None

    view = discord.ui.View(timeout=None)

    if profile_url:
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="Subscribe",
                emoji="🔔",
                url=profile_url,
            )
        )

    if open_url:
        open_label = "Watch Now" if platform in {"youtube", "tiktok"} else "Read Now"
        open_emoji = "▶️" if platform in {"youtube", "tiktok"} else "📰"
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label=open_label,
                emoji=open_emoji,
                url=open_url,
            )
        )

    return view


def build_social_embed(monitor: dict, entry: dict) -> discord.Embed:
    platform = monitor.get("platform") or "rss"
    title = str(entry.get("title") or "New post")[:240]
    link = entry.get("link")
    source_name = monitor.get("name") or entry.get("author") or monitor.get("source") or "Configured source"
    published_at = entry.get("published_at") or datetime.now(timezone.utc)

    is_video_platform = str(platform).lower() in {"youtube", "tiktok"}
    item_label = "video" if is_video_platform else "post"

    embed = discord.Embed(
        title=title,
        url=link if link else None,
        color=_social_platform_color(platform),
        timestamp=published_at,
    )
    embed.description = f"**{source_name}** just dropped a new {item_label}!"

    thumb = entry.get("thumbnail")
    if isinstance(thumb, str) and thumb.startswith("http"):
        embed.set_image(url=thumb)

    embed.set_footer(text=f"Published on {_social_platform_label(platform)}")
    return embed


@tasks.loop(minutes=1)
async def social_update_check():
    started_at = datetime.now(timezone.utc)
    notifications_sent = 0
    monitor_count = 0

    # Run blocking JSON I/O in a thread so the event loop (and Discord heartbeat) stay free.
    await asyncio.to_thread(
        set_social_health,
        last_poll_started=started_at.isoformat(),
        last_error=None,
    )

    try:
        monitors = await asyncio.to_thread(load_social_monitors)
        changed = False

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25),
            headers={"User-Agent": "SeisenHubBot/1.0 (+social-monitor)"},
        ) as session:
            for guild_id, cfgs in monitors.items():
                for cfg in cfgs:
                    if not cfg.get("enabled", True):
                        continue

                    monitor_count += 1
                    cfg["last_checked_at"] = datetime.now(timezone.utc).isoformat()
                    changed = True

                    try:
                        platform = str(cfg.get("platform") or "rss").strip().lower()
                        source = str(cfg.get("source") or "").strip()
                        if platform == "youtube":
                            _prev = str(cfg.get("last_entry_id") or "").strip() or None
                            resolved_source_url, latest = await fetch_youtube_latest_entry(source, session, previous_id=_prev)
                            latest["source_url"] = resolved_source_url
                        else:
                            source_url = resolve_social_source_url(platform, source)

                            if not source_url:
                                cfg["last_error"] = (
                                    "Could not resolve source URL. Use a valid URL, or for TikTok use @handle."
                                )
                                continue

                            latest = await fetch_source_latest_entry(source_url, session)
                            if latest is not None:
                                latest["source_url"] = source_url

                        if not latest:
                            cfg["last_error"] = "Source resolved but no entries were found yet."
                            continue

                        latest_id = str(latest.get("id") or "").strip()
                        if not latest_id:
                            cfg["last_error"] = "Latest source entry is missing a stable id."
                            continue

                        previous_id = str(cfg.get("last_entry_id") or "").strip()
                        if not previous_id:
                            # Seed without posting to avoid flooding old uploads on first setup.
                            cfg["last_entry_id"] = latest_id
                            cfg["last_error"] = None
                            continue

                        if previous_id == latest_id:
                            cfg["last_error"] = None
                            continue

                        guild_int = _as_int(guild_id)
                        channel_id = _as_int(cfg.get("channel_id"))
                        if not guild_int or not channel_id:
                            cfg["last_error"] = "Missing guild or channel id in monitor settings."
                            continue

                        guild_obj = bot.get_guild(guild_int)
                        if not guild_obj:
                            cfg["last_error"] = "Bot is not in the configured guild."
                            continue

                        channel = guild_obj.get_channel(channel_id)
                        if channel is None:
                            try:
                                channel = await guild_obj.fetch_channel(channel_id)
                            except Exception:
                                channel = None
                        if not channel:
                            cfg["last_error"] = "Configured channel no longer exists."
                            continue
                        if not hasattr(channel, "send"):
                            cfg["last_error"] = "Configured channel does not support sending messages."
                            continue

                        role_id = _as_int(cfg.get("role_id"))
                        role = guild_obj.get_role(role_id) if role_id else None

                        content = role.mention if role else None

                        embed = build_social_embed(cfg, latest)
                        view = build_social_action_view(cfg, latest)

                        monitor_name = cfg.get("name") or source
                        print(f"[Social Monitor] Posting new video for '{monitor_name}': {latest.get('title')} ({latest_id})")

                        await channel.send(
                            content=content,
                            embed=embed,
                            view=view,
                            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
                        )

                        # Update tracking state immediately after a successful send.
                        # This is its own block so a future error can't prevent the save.
                        cfg["last_entry_id"] = latest_id
                        cfg["last_posted_at"] = datetime.now(timezone.utc).isoformat()
                        cfg["last_error"] = None
                        notifications_sent += 1

                        # Save immediately so a bot crash after this point doesn't
                        # cause the same video to be re-posted on restart.
                        await asyncio.to_thread(save_social_monitors, monitors)
                        print(f"[Social Monitor] Saved last_entry_id={latest_id} for '{monitor_name}'")

                    except Exception as monitor_err:
                        cfg["last_error"] = _sanitize_social_error(f"{type(monitor_err).__name__}: {monitor_err}")
                        print(f"[Social Monitor] Error for '{cfg.get('name') or source}': {monitor_err}")

        if changed:
            await asyncio.to_thread(save_social_monitors, monitors)


    except Exception as e:
        await asyncio.to_thread(
            set_social_health,
            last_error=_sanitize_social_error(f"{type(e).__name__}: {e}"),
        )
        print(f"[Social Monitor] Loop error: {e}")

    finally:
        finished_at = datetime.now(timezone.utc)
        await asyncio.to_thread(
            set_social_health,
            last_poll_finished=finished_at.isoformat(),
            last_poll_seconds=round((finished_at - started_at).total_seconds(), 2),
            last_notifications=notifications_sent,
            monitor_count=monitor_count,
        )


@social_update_check.before_loop
async def before_social_update_check():
    await bot.wait_until_ready()
    await asyncio.to_thread(
        set_social_health,
        loop_started_at=datetime.now(timezone.utc).isoformat(),
        last_error=None,
    )


def register(bot_instance):
    global bot
    bot = bot_instance


