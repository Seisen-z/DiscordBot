"""
modules/music.py
Music player — yt-dlp + FFmpeg streaming (no Lavalink, no temp files).
Designed for 512 MiB hosts; supports up to ~2 simultaneous guild streams.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

_YTDL_AVAILABLE = False
try:
    import yt_dlp
    _YTDL_AVAILABLE = True
except ImportError:
    pass

# ── Locate ffmpeg ─────────────────────────────────────────────────────────────

def _find_ffmpeg() -> str:
    import shutil, glob, sys, os

    # 1. System PATH (Linux servers, most prod environments)
    if shutil.which("ffmpeg"):
        return "ffmpeg"

    # 2. Windows — winget installs to a versioned folder under WinGet\Packages
    if sys.platform == "win32":
        winget_base = os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Microsoft", "WinGet", "Packages",
        )
        matches = glob.glob(os.path.join(winget_base, "Gyan.FFmpeg*", "**", "ffmpeg.exe"), recursive=True)
        if matches:
            return matches[0]

    # 3. imageio-ffmpeg — pip-installable static binary, works in Docker/Pterodactyl
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe:
            print(f"[Music] Using imageio-ffmpeg: {exe}")
            return exe
    except Exception:
        pass

    return "ffmpeg"  # last resort — will give a clear error at play-time if missing


_FFMPEG_EXE = _find_ffmpeg()

# ── Config ────────────────────────────────────────────────────────────────────

_YTDL_OPTS: dict = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
}

_FFMPEG_OPTS: dict = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -bufsize 512k",
}

_IDLE_TIMEOUT  = 300
_ALONE_TIMEOUT = 60
_MAX_QUEUE     = 50
_TITLE_LIMIT   = 60
_COLOR         = 0x5865F2

# ── Track ─────────────────────────────────────────────────────────────────────

class Track:
    __slots__ = ("stream_url", "title", "webpage_url", "duration",
                 "thumbnail", "uploader", "requester")

    def __init__(
        self,
        stream_url: str,
        title: str,
        webpage_url: str,
        duration: int,
        thumbnail: Optional[str],
        uploader: str,
        requester: discord.Member,
    ) -> None:
        self.stream_url  = stream_url
        self.title       = title
        self.webpage_url = webpage_url
        self.duration    = duration
        self.thumbnail   = thumbnail
        self.uploader    = uploader
        self.requester   = requester

    @property
    def duration_str(self) -> str:
        d = int(self.duration or 0)
        if not d:
            return "Live / Unknown"
        m, s = divmod(d, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    @property
    def display_title(self) -> str:
        t = self.title
        return (t[:_TITLE_LIMIT] + "…") if len(t) > _TITLE_LIMIT else t


# ── Per-guild player state ────────────────────────────────────────────────────

_LOOP_CYCLE = ("off", "song", "queue")

_LOOP_META = {
    "off":   ("⬛ Off",       discord.Color.dark_gray(),  "Playback stops when the queue ends."),
    "song":  ("🔂 Song Loop", discord.Color.blurple(),    "Current track repeats indefinitely."),
    "queue": ("🔁 Queue Loop", discord.Color.green(),     "All tracks loop — played songs cycle back to the end of the queue."),
}


class GuildPlayer:
    def __init__(self) -> None:
        self.queue:     deque[Track]                      = deque()
        self.current:   Optional[Track]                   = None
        self.loop_mode: str                               = "off"
        self.volume:    float                             = 0.5
        self.vc:        Optional[discord.VoiceClient]     = None
        self.channel:   Optional[discord.abc.Messageable] = None
        self._idle_task: Optional[asyncio.Task]           = None

    def reset_idle(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def clear(self) -> None:
        self.queue.clear()
        self.current = None


_players: Dict[int, GuildPlayer] = {}
_bot_ref:  Optional[commands.Bot] = None


def _player(guild_id: int) -> GuildPlayer:
    if guild_id not in _players:
        _players[guild_id] = GuildPlayer()
    return _players[guild_id]


# ── yt-dlp resolver ───────────────────────────────────────────────────────────

async def _resolve_track(query: str, requester: discord.Member) -> Optional[Track]:
    """
    Resolve a search query or URL to a Track.
    For text queries we use ytsearch1: to get one result, then re-extract the
    proper YouTube watch URL so yt-dlp returns the real title/duration/thumbnail
    instead of a raw stream URL like 'videoplayback'.
    """
    loop = asyncio.get_event_loop()

    def _extract() -> Optional[dict]:
        is_url = query.startswith("http://") or query.startswith("https://")
        # For text searches, restrict to 1 result for speed
        search_input = query if is_url else f"ytsearch1:{query}"

        with yt_dlp.YoutubeDL(_YTDL_OPTS) as ydl:
            raw = ydl.extract_info(search_input, download=False)
            if not raw:
                return None

            # Search returns a wrapper dict with 'entries'
            if "entries" in raw:
                entries = raw.get("entries") or []
                if not entries:
                    return None
                first = entries[0]
                video_id = first.get("id")
                # Always rebuild a clean YouTube watch URL — never pass the raw
                # stream URL back to yt-dlp or it returns 'videoplayback' as title.
                if video_id:
                    watch_url = f"https://www.youtube.com/watch?v={video_id}"
                elif first.get("webpage_url"):
                    watch_url = first["webpage_url"]
                else:
                    return None
                raw = ydl.extract_info(watch_url, download=False)

            return raw

    try:
        info = await loop.run_in_executor(None, _extract)
    except Exception:
        return None

    if not info:
        return None

    # Prefer an audio-only format to save memory/bandwidth
    stream_url: str = info.get("url") or ""
    for fmt in (info.get("formats") or []):
        if fmt.get("vcodec") == "none" and fmt.get("acodec") != "none":
            stream_url = fmt.get("url", stream_url)
            break

    if not stream_url:
        return None

    # Pick the highest-resolution thumbnail available
    thumbnail: Optional[str] = info.get("thumbnail")
    thumbnails = info.get("thumbnails") or []
    if thumbnails:
        best = max(
            (t for t in thumbnails if t.get("url")),
            key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
            default=None,
        )
        if best:
            thumbnail = best["url"]

    return Track(
        stream_url  = stream_url,
        title       = info.get("title") or "Unknown",
        webpage_url = info.get("webpage_url") or query,
        duration    = int(info.get("duration") or 0),
        thumbnail   = thumbnail,
        uploader    = info.get("uploader") or info.get("channel") or "",
        requester   = requester,
    )


# ── Embed builders ────────────────────────────────────────────────────────────

def _np_embed(track: Track, player: GuildPlayer, label: str = "Now Playing") -> discord.Embed:
    embed = discord.Embed(color=_COLOR)
    embed.set_author(name=f"🎵 {label}")
    embed.title = track.display_title
    embed.url   = track.webpage_url

    if track.uploader:
        embed.description = f"by **{track.uploader}**"

    loop_label, _, _ = _LOOP_META.get(player.loop_mode, _LOOP_META["off"])

    embed.add_field(name="⏱ Duration",     value=track.duration_str,           inline=True)
    embed.add_field(name="🔊 Volume",       value=f"{int(player.volume*100)}%",  inline=True)
    embed.add_field(name="🔁 Loop",         value=loop_label,                    inline=True)
    embed.add_field(name="👤 Requested by", value=track.requester.mention,       inline=True)

    q_size = len(player.queue)
    if q_size:
        embed.add_field(name="📋 Up next", value=f"{q_size} track{'s' if q_size != 1 else ''} in queue", inline=True)

    if track.thumbnail:
        embed.set_image(url=track.thumbnail)

    embed.set_footer(text="Use /queue to see all tracks • /skip to skip • /stop to stop")
    return embed


def _queued_embed(track: Track, position: int, player: GuildPlayer) -> discord.Embed:
    embed = discord.Embed(color=_COLOR)
    embed.set_author(name="✅ Added to Queue")
    embed.title = track.display_title
    embed.url   = track.webpage_url

    if track.uploader:
        embed.description = f"by **{track.uploader}**"

    embed.add_field(name="⏱ Duration",      value=track.duration_str,      inline=True)
    embed.add_field(name="📋 Position",      value=f"#{position}",           inline=True)
    embed.add_field(name="👤 Requested by",  value=track.requester.mention,  inline=True)

    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)

    return embed


# ── Playback engine ───────────────────────────────────────────────────────────

def _play_track(p: GuildPlayer, track: Track, guild_id: int) -> None:
    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(track.stream_url, executable=_FFMPEG_EXE, **_FFMPEG_OPTS),
        volume=p.volume,
    )
    p.vc.play(source, after=lambda _: _advance(guild_id))


def _advance(guild_id: int) -> None:
    """After-callback (worker thread) — pop next track or schedule idle disconnect."""
    p = _player(guild_id)
    p.reset_idle()

    if p.loop_mode == "song" and p.current:
        p.queue.appendleft(p.current)   # re-insert at front → same song plays again
    elif p.loop_mode == "queue" and p.current:
        p.queue.append(p.current)       # re-insert at back → cycles through whole queue

    if not p.queue:
        p.current = None
        if _bot_ref and p.vc and p.vc.is_connected():
            asyncio.run_coroutine_threadsafe(_idle_then_disconnect(guild_id), _bot_ref.loop)
        return

    next_track = p.queue.popleft()
    p.current   = next_track

    if not (p.vc and p.vc.is_connected()):
        p.current = None
        return

    _play_track(p, next_track, guild_id)

    if _bot_ref and p.channel:
        asyncio.run_coroutine_threadsafe(
            p.channel.send(embed=_np_embed(next_track, p)), _bot_ref.loop
        )


async def _begin(guild_id: int, track: Track) -> None:
    p = _player(guild_id)
    p.reset_idle()
    p.current = track
    if p.vc and p.vc.is_connected():
        _play_track(p, track, guild_id)


async def _idle_then_disconnect(guild_id: int) -> None:
    p = _player(guild_id)
    try:
        p._idle_task = asyncio.current_task()
        await asyncio.sleep(_IDLE_TIMEOUT)
        if p.vc and p.vc.is_connected() and not p.current:
            await p.vc.disconnect()
            p.vc = None
            if p.channel:
                await p.channel.send("👋 Disconnected after being idle for 5 minutes.")
    except asyncio.CancelledError:
        pass


# ── Voice-state listener ──────────────────────────────────────────────────────

def _setup_listeners(bot: commands.Bot) -> None:
    @bot.listen("on_voice_state_update")
    async def _music_vsu(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        guild_id = member.guild.id
        p = _players.get(guild_id)
        if not p or not p.vc or not p.vc.is_connected():
            return

        if member.id == bot.user.id and after.channel is None:
            p.reset_idle()
            p.clear()
            p.vc = None
            return

        if before.channel and before.channel.id == p.vc.channel.id:
            humans = [m for m in p.vc.channel.members if not m.bot]
            if not humans:
                if p.vc.is_playing() or p.vc.is_paused():
                    p.vc.pause()
                if p.channel:
                    await p.channel.send(
                        f"⏸ Everyone left — pausing. Resuming if someone rejoins within {_ALONE_TIMEOUT}s."
                    )
                await asyncio.sleep(_ALONE_TIMEOUT)
                p2 = _players.get(guild_id)
                if p2 and p2.vc and p2.vc.is_connected():
                    humans2 = [m for m in p2.vc.channel.members if not m.bot]
                    if not humans2:
                        p2.reset_idle()
                        p2.loop = False
                        p2.clear()
                        if p2.vc.is_playing() or p2.vc.is_paused():
                            p2.vc.stop()
                        await p2.vc.disconnect()
                        p2.vc = None
                        if p2.channel:
                            await p2.channel.send("👋 Nobody rejoined — disconnected.")

        elif after.channel and p.vc and after.channel.id == p.vc.channel.id:
            if p.vc.is_paused() and p.current:
                p.vc.resume()
                if p.channel:
                    await p.channel.send(f"▶️ {member.mention} rejoined — resuming.")


# ── Register ──────────────────────────────────────────────────────────────────

def register(bot: commands.Bot) -> None:
    global _bot_ref
    _bot_ref = bot

    if not _YTDL_AVAILABLE:
        print("[Music] yt-dlp not installed — music commands disabled.")
        print("[Music] Install with: pip install yt-dlp PyNaCl")
        return

    _setup_listeners(bot)

    # ── /play ──────────────────────────────────────────────────────────────────

    @bot.tree.command(name="play", description="Play a song — YouTube URL or search terms")
    @app_commands.guild_only()
    @app_commands.describe(query="YouTube URL or search terms")
    async def cmd_play(interaction: discord.Interaction, query: str) -> None:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "❌ You need to be in a voice channel first.", ephemeral=True
            )
            return

        voice_channel = interaction.user.voice.channel
        await interaction.response.defer(thinking=True)

        p = _player(interaction.guild_id)

        if p.vc and p.vc.is_connected():
            if p.vc.channel.id != voice_channel.id:
                await p.vc.move_to(voice_channel)
        else:
            try:
                p.vc = await voice_channel.connect()
            except Exception as exc:
                await interaction.followup.send(f"❌ Could not join voice: {exc}")
                return

        p.channel = interaction.channel

        track = await _resolve_track(query, interaction.user)
        if not track:
            await interaction.followup.send(
                "❌ Could not find or load that track. Try a different search or a direct YouTube URL."
            )
            return

        if p.vc.is_playing() or p.vc.is_paused():
            if len(p.queue) >= _MAX_QUEUE:
                await interaction.followup.send(f"❌ Queue is full ({_MAX_QUEUE} tracks max).", ephemeral=True)
                return
            p.queue.append(track)
            await interaction.followup.send(embed=_queued_embed(track, len(p.queue), p))
        else:
            await _begin(interaction.guild_id, track)
            await interaction.followup.send(embed=_np_embed(track, p))

    # ── /pause ─────────────────────────────────────────────────────────────────

    @bot.tree.command(name="pause", description="Pause the current track")
    @app_commands.guild_only()
    async def cmd_pause(interaction: discord.Interaction) -> None:
        p = _player(interaction.guild_id)
        if p.vc and p.vc.is_playing():
            p.vc.pause()
            await interaction.response.send_message("⏸ Paused.")
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    # ── /resume ────────────────────────────────────────────────────────────────

    @bot.tree.command(name="resume", description="Resume a paused track")
    @app_commands.guild_only()
    async def cmd_resume(interaction: discord.Interaction) -> None:
        p = _player(interaction.guild_id)
        if p.vc and p.vc.is_paused():
            p.vc.resume()
            await interaction.response.send_message("▶️ Resumed.")
        else:
            await interaction.response.send_message("❌ Nothing is paused.", ephemeral=True)

    # ── /skip ──────────────────────────────────────────────────────────────────

    @bot.tree.command(name="skip", description="Skip the current track")
    @app_commands.guild_only()
    async def cmd_skip(interaction: discord.Interaction) -> None:
        p = _player(interaction.guild_id)
        if p.vc and (p.vc.is_playing() or p.vc.is_paused()):
            # Temporarily disable song loop so skip actually moves to next track.
            # Queue loop is preserved — skipped track still cycles to the back.
            if p.loop_mode == "song":
                p.loop_mode = "off"
            p.vc.stop()
            await interaction.response.send_message("⏭ Skipped.")
        else:
            await interaction.response.send_message("❌ Nothing to skip.", ephemeral=True)

    # ── /stop ──────────────────────────────────────────────────────────────────

    @bot.tree.command(name="stop", description="Stop playback, clear the queue and disconnect")
    @app_commands.guild_only()
    async def cmd_stop(interaction: discord.Interaction) -> None:
        p = _player(interaction.guild_id)
        if not p.vc or not p.vc.is_connected():
            await interaction.response.send_message("❌ I'm not in a voice channel.", ephemeral=True)
            return
        p.reset_idle()
        p.loop_mode = "off"
        p.clear()
        if p.vc.is_playing() or p.vc.is_paused():
            p.vc.stop()
        await p.vc.disconnect()
        p.vc = None
        await interaction.response.send_message("⏹ Stopped and disconnected.")

    # ── /queue ─────────────────────────────────────────────────────────────────

    @bot.tree.command(name="queue", description="Show the music queue")
    @app_commands.guild_only()
    async def cmd_queue(interaction: discord.Interaction) -> None:
        p = _player(interaction.guild_id)
        if not p.current and not p.queue:
            await interaction.response.send_message("📭 The queue is empty.", ephemeral=True)
            return

        embed = discord.Embed(title="📋 Music Queue", color=_COLOR)
        lines: list[str] = []

        if p.current:
            loop_label, _, _ = _LOOP_META.get(p.loop_mode, _LOOP_META["off"])
            tag = f" — {loop_label}" if p.loop_mode != "off" else ""
            lines.append(
                f"**▶ Now Playing{tag}**\n"
                f"[{p.current.display_title}]({p.current.webpage_url}) "
                f"`{p.current.duration_str}` — {p.current.requester.mention}"
            )
            if p.current.thumbnail:
                embed.set_thumbnail(url=p.current.thumbnail)

        q = list(p.queue)
        if q:
            lines.append("")
            for i, t in enumerate(q[:15], 1):
                lines.append(
                    f"`{i:02d}.` [{t.display_title}]({t.webpage_url}) "
                    f"`{t.duration_str}` — {t.requester.mention}"
                )
            if len(q) > 15:
                lines.append(f"\n*…and {len(q) - 15} more tracks*")

        embed.description = "\n".join(lines)
        embed.set_footer(text=f"{len(q)} track{'s' if len(q) != 1 else ''} waiting • Volume: {int(p.volume*100)}%")
        await interaction.response.send_message(embed=embed)

    # ── /removequeue ───────────────────────────────────────────────────────────

    @bot.tree.command(name="removequeue", description="Remove a track from the queue by its position number")
    @app_commands.guild_only()
    @app_commands.describe(position="Queue position to remove (use /queue to see numbers)")
    async def cmd_removequeue(interaction: discord.Interaction, position: int) -> None:
        p = _player(interaction.guild_id)
        if not p.queue:
            await interaction.response.send_message("📭 The queue is empty.", ephemeral=True)
            return
        if not 1 <= position <= len(p.queue):
            await interaction.response.send_message(
                f"❌ Invalid position. Pick a number between **1** and **{len(p.queue)}**.",
                ephemeral=True,
            )
            return

        q_list = list(p.queue)
        removed = q_list.pop(position - 1)
        p.queue = deque(q_list)

        embed = discord.Embed(
            description=f"🗑️ Removed **[{removed.display_title}]({removed.webpage_url})** from position **#{position}**",
            color=_COLOR,
        )
        embed.set_footer(text=f"{len(p.queue)} track{'s' if len(p.queue) != 1 else ''} remaining in queue")
        await interaction.response.send_message(embed=embed)

    # ── /nowplaying ────────────────────────────────────────────────────────────

    @bot.tree.command(name="nowplaying", description="Show details about the current track")
    @app_commands.guild_only()
    async def cmd_nowplaying(interaction: discord.Interaction) -> None:
        p = _player(interaction.guild_id)
        if not p.current:
            await interaction.response.send_message("❌ Nothing is playing right now.", ephemeral=True)
            return
        await interaction.response.send_message(embed=_np_embed(p.current, p))

    # ── /volume ────────────────────────────────────────────────────────────────

    @bot.tree.command(name="volume", description="Set the playback volume (0–100)")
    @app_commands.guild_only()
    @app_commands.describe(level="Volume level between 0 and 100")
    async def cmd_volume(interaction: discord.Interaction, level: int) -> None:
        if not 0 <= level <= 100:
            await interaction.response.send_message("❌ Volume must be between 0 and 100.", ephemeral=True)
            return
        p = _player(interaction.guild_id)
        p.volume = level / 100
        if p.vc and hasattr(p.vc.source, "volume"):
            p.vc.source.volume = p.volume
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")

    # ── /loop ──────────────────────────────────────────────────────────────────

    @bot.tree.command(name="loop", description="Cycle loop mode: Off → Song → Queue → Off")
    @app_commands.guild_only()
    async def cmd_loop(interaction: discord.Interaction) -> None:
        p = _player(interaction.guild_id)
        current_idx = _LOOP_CYCLE.index(p.loop_mode) if p.loop_mode in _LOOP_CYCLE else 0
        p.loop_mode = _LOOP_CYCLE[(current_idx + 1) % len(_LOOP_CYCLE)]

        label, color, description = _LOOP_META[p.loop_mode]

        embed = discord.Embed(color=color)
        embed.set_author(name="🔁 Loop Mode Updated")
        embed.add_field(name="Active Mode", value=f"**{label}**", inline=False)
        embed.add_field(name="What this means", value=description, inline=False)

        cycle_parts = []
        for mode in _LOOP_CYCLE:
            m_label, _, _ = _LOOP_META[mode]
            cycle_parts.append(f"**{m_label}**" if mode == p.loop_mode else m_label)
        embed.add_field(name="Cycle", value=" → ".join(cycle_parts), inline=False)

        if p.current:
            embed.set_footer(text=f"Currently playing: {p.current.display_title}")

        await interaction.response.send_message(embed=embed)

    # ── /disconnect ────────────────────────────────────────────────────────────

    @bot.tree.command(name="disconnect", description="Disconnect the bot from voice")
    @app_commands.guild_only()
    async def cmd_disconnect(interaction: discord.Interaction) -> None:
        p = _player(interaction.guild_id)
        if not p.vc or not p.vc.is_connected():
            await interaction.response.send_message("❌ Not connected to a voice channel.", ephemeral=True)
            return
        p.reset_idle()
        p.loop_mode = "off"
        p.clear()
        if p.vc.is_playing() or p.vc.is_paused():
            p.vc.stop()
        await p.vc.disconnect()
        p.vc = None
        await interaction.response.send_message("👋 Disconnected.")

    print("[Music] Music commands registered (yt-dlp streaming mode).")
