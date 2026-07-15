"""
modules/polls.py
Poll system – native Discord polls with slash commands.
"""

from __future__ import annotations

import discord
from discord import app_commands
from datetime import datetime, timedelta, timezone

from modules.utils import load_json, save_json, _as_int

# Runtime poll instances (per guild → message id). Drafts live under ``poll_drafts`` (API only).
POLL_STATE_FILE = "polls"


# ── Helpers ───────────────────────────────────────────────────────────────────

def clamp_poll_duration_hours(value) -> int:
    try:
        h = int(value)
    except (TypeError, ValueError):
        h = 24
    return max(1, min(168, h))


def normalize_poll_choices(raw: list) -> list[str]:
    seen: set[str] = set()
    choices: list[str] = []
    for item in raw:
        text = str(item or "").strip()[:55]
        if text and text not in seen:
            seen.add(text)
            choices.append(text)
        if len(choices) == 10:
            break
    return choices


def load_poll_state() -> dict:
    data = load_json(POLL_STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def save_poll_state(data: dict):
    save_json(POLL_STATE_FILE, data)


def upsert_poll_state_entry(
    guild_id: int,
    channel_id: int,
    message: discord.Message,
    question: str,
    choices: list[str],
    duration_hours: int,
    multiple: bool,
    created_by_user_id: int | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    duration = clamp_poll_duration_hours(duration_hours)
    clean_choices = normalize_poll_choices(choices)

    guild_key = str(guild_id)
    message_key = str(message.id)
    channel_key = str(channel_id)

    data = load_poll_state()
    guild_entries = data.setdefault(guild_key, {})
    existing = guild_entries.get(message_key) if isinstance(guild_entries, dict) else None
    if not isinstance(guild_entries, dict):
        guild_entries = {}
        data[guild_key] = guild_entries
    if not isinstance(existing, dict):
        existing = {}

    entry = {
        "guild_id": guild_key,
        "channel_id": channel_key,
        "message_id": message_key,
        "question": str(question or "").strip()[:300],
        "options": clean_choices,
        "duration_hours": duration,
        "multiple": bool(multiple),
        "created_at": existing.get("created_at") or now.isoformat(),
        "end_at": existing.get("end_at") or (now + timedelta(hours=duration)).isoformat(),
        "ended": bool(existing.get("ended", False)),
        "ended_at": existing.get("ended_at"),
        "ended_by": existing.get("ended_by"),
        "ended_reason": existing.get("ended_reason"),
        "jump_url": str(getattr(message, "jump_url", "") or "") or None,
    }
    if created_by_user_id is not None and not existing.get("created_by"):
        entry["created_by"] = str(created_by_user_id)
    elif existing.get("created_by"):
        entry["created_by"] = str(existing.get("created_by"))

    guild_entries[message_key] = entry
    save_poll_state(data)
    return entry


def get_poll_state_entry(guild_id: int, message_id: int) -> dict | None:
    data = load_poll_state()
    guild_entries = data.get(str(guild_id), {})
    if not isinstance(guild_entries, dict):
        return None
    entry = guild_entries.get(str(message_id))
    return entry if isinstance(entry, dict) else None


def mark_poll_state_ended(
    guild_id: int,
    message_id: int,
    ended_by_user_id: int | None = None,
    reason: str = "manual",
) -> dict | None:
    data = load_poll_state()
    guild_key = str(guild_id)
    message_key = str(message_id)

    guild_entries = data.get(guild_key)
    if not isinstance(guild_entries, dict):
        return None

    entry = guild_entries.get(message_key)
    if not isinstance(entry, dict):
        return None

    entry["ended"] = True
    entry["ended_at"] = datetime.now(timezone.utc).isoformat()
    if ended_by_user_id is not None:
        entry["ended_by"] = str(ended_by_user_id)
    entry["ended_reason"] = str(reason or "manual")

    guild_entries[message_key] = entry
    data[guild_key] = guild_entries
    save_poll_state(data)
    return entry


def _extract_poll_question_text(poll: object) -> str:
    question_obj = getattr(poll, "question", None)
    if isinstance(question_obj, str):
        return question_obj.strip()
    text = getattr(question_obj, "text", None)
    if isinstance(text, str):
        return text.strip()
    return ""


def _extract_poll_options(poll: object) -> list[str]:
    options: list[str] = []
    answers = getattr(poll, "answers", None)
    if not answers:
        return options

    for answer in answers:
        text = getattr(answer, "text", None)
        if not text:
            media = getattr(answer, "media", None)
            text = getattr(media, "text", None)
        value = str(text or "").strip()
        if value:
            options.append(value)
    return normalize_poll_choices(options)


async def _resolve_channel(guild: discord.Guild, channel_id: int) -> discord.abc.GuildChannel | None:
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            channel = None
    return channel


async def end_native_poll(
    guild: discord.Guild,
    message_id: int,
    channel_id: int | None = None,
    ended_by_user_id: int | None = None,
    reason: str = "manual",
) -> tuple[bool, str, dict | None]:
    state_entry = get_poll_state_entry(guild.id, message_id)
    resolved_channel_id = channel_id
    if resolved_channel_id is None and state_entry:
        resolved_channel_id = _as_int(state_entry.get("channel_id"))

    if not resolved_channel_id:
        return False, "Missing channel_id for poll. Provide channel_id or create poll from dashboard first.", state_entry

    channel = await _resolve_channel(guild, resolved_channel_id)
    if not channel or not hasattr(channel, "fetch_message"):
        return False, f"Channel {resolved_channel_id} not found.", state_entry

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        return False, f"Poll message {message_id} was not found in channel {resolved_channel_id}.", state_entry
    except Exception as fetch_err:
        return False, f"Failed to fetch poll message: {fetch_err}", state_entry

    poll_obj = getattr(message, "poll", None)
    if poll_obj is None:
        return False, "Target message does not contain a Discord poll.", state_entry

    if state_entry is None:
        inferred_question = _extract_poll_question_text(poll_obj)
        inferred_options = _extract_poll_options(poll_obj)
        inferred_multiple = bool(getattr(poll_obj, "multiple", False))
        state_entry = upsert_poll_state_entry(
            guild_id=guild.id,
            channel_id=resolved_channel_id,
            message=message,
            question=inferred_question or "Discord Poll",
            choices=inferred_options,
            duration_hours=24,
            multiple=inferred_multiple,
            created_by_user_id=ended_by_user_id,
        )

    is_finalized = bool(getattr(poll_obj, "is_finalized", False))
    if not is_finalized:
        try:
            if hasattr(message, "end_poll"):
                await message.end_poll()
            elif hasattr(poll_obj, "end"):
                await poll_obj.end()  # type: ignore[attr-defined]
            else:
                return False, "This discord.py build does not support ending polls manually.", state_entry
        except Exception as end_err:
            return False, f"Failed to end poll: {end_err}", state_entry

    ended_entry = mark_poll_state_ended(
        guild.id,
        message_id,
        ended_by_user_id=ended_by_user_id,
        reason=reason,
    )
    if ended_entry is None:
        ended_entry = state_entry

    if is_finalized:
        return True, "Poll was already ended.", ended_entry
    return True, "Poll ended.", ended_entry


async def send_native_poll(
    channel: discord.TextChannel,
    question: str,
    choices: list[str],
    duration_hours: int,
    multiple: bool,
) -> discord.Message:
    poll = discord.Poll(
        question=question,
        duration=timedelta(hours=duration_hours),
        multiple=multiple,
    )
    for choice in choices:
        poll.add_answer(text=str(choice))
    return await channel.send(poll=poll)


# ── Slash commands ────────────────────────────────────────────────────────────

poll_group = app_commands.Group(name="poll", description="Poll commands")


@poll_group.command(name="create", description="Start a native Discord poll with live results")
@app_commands.describe(
    question="The poll question",
    choice1="Option 1",
    choice2="Option 2",
    choice3="Option 3 (optional)",
    choice4="Option 4 (optional)",
    choice5="Option 5 (optional)",
    choice6="Option 6 (optional)",
    choice7="Option 7 (optional)",
    choice8="Option 8 (optional)",
    choice9="Option 9 (optional)",
    choice10="Option 10 (optional)",
    duration_hours="How long the poll runs (1–168 h, default 24)",
    multiple="Allow multiple selections (default False)",
)
async def poll_create(
    interaction: discord.Interaction,
    question: str,
    choice1: str,
    choice2: str,
    choice3: str = None,
    choice4: str = None,
    choice5: str = None,
    choice6: str = None,
    choice7: str = None,
    choice8: str = None,
    choice9: str = None,
    choice10: str = None,
    duration_hours: app_commands.Range[int, 1, 168] = 24,
    multiple: bool = False,
):
    raw = [choice1, choice2, choice3, choice4, choice5,
           choice6, choice7, choice8, choice9, choice10]
    choices = normalize_poll_choices(raw)

    if len(choices) < 2:
        await interaction.response.send_message(
            "❌ Please provide at least 2 unique options.", ephemeral=True
        )
        return

    await interaction.response.defer()
    try:
        msg = await send_native_poll(
            interaction.channel,
            question=question,
            choices=choices,
            duration_hours=duration_hours,
            multiple=multiple,
        )
        await interaction.followup.send(
            f"✅ Poll created! [Jump to poll]({msg.jump_url})", ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to create poll: {e}", ephemeral=True)


def register(bot: discord.ext.commands.Bot):
    """Register poll commands with the bot tree."""
    bot.tree.add_command(poll_group)


@poll_group.command(name="end", description="Manually end a native Discord poll")
@app_commands.describe(
    message_id="Poll message ID",
    channel="Channel containing the poll (defaults to current channel)",
)
@app_commands.checks.has_permissions(manage_messages=True)
async def poll_end(
    interaction: discord.Interaction,
    message_id: str,
    channel: discord.TextChannel | None = None,
):
    parsed_message_id = _as_int(message_id)
    if not parsed_message_id:
        await interaction.response.send_message("❌ Invalid poll message ID.", ephemeral=True)
        return

    current_channel_id = getattr(interaction.channel, "id", None)
    channel_id = channel.id if channel else _as_int(current_channel_id)
    if not channel_id:
        await interaction.response.send_message(
            "❌ Could not determine channel. Provide the channel explicitly.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    success, message, _entry = await end_native_poll(
        interaction.guild,
        parsed_message_id,
        channel_id=channel_id,
        ended_by_user_id=interaction.user.id,
        reason="slash",
    )

    if not success:
        await interaction.followup.send(f"❌ {message}", ephemeral=True)
        return

    await interaction.followup.send(f"✅ {message}", ephemeral=True)

