"""
modules/utils.py
Shared helpers used across all feature modules.
"""

import copy
import json
import os
import re
import threading
import tempfile
from pathlib import Path
import discord
from datetime import datetime, timezone


# ── Config file path constants ────────────────────────────────────────────────

GIVEAWAYS_FILE = "giveaways"
POLL_STATE_FILE = "poll_drafts"
MEMBER_COUNTER_FILE = "member_counter_configs"
COMMAND_ACCESS_FILE = "command_access"
BOOST_CONFIG_FILE = "boost_configs"
VOUCH_CONFIG_FILE = "vouch_configs"
ONBOARDING_CONFIG_FILE = "onboarding_configs"
ONBOARDING_STATE_FILE = "onboarding_verification_states"
ANNOUNCE_DRAFTS_FILE = "announcement_drafts"
REACTION_ROLES_FILE = "reaction_roles"
SELECT_MENU_ROLES_FILE = "select_menu_roles"
AUTOREPLY_FILE = "autoreplies"
AI_HELP_FILE = "ai_help_global"
TICKET_FILE = "open_tickets"
ROBLOX_FILE = "roblox_monitors"
ROBLOX_HEALTH_FILE = "roblox_monitor_health"
SOCIAL_FILE = "social_monitors"
SOCIAL_HEALTH_FILE = "social_monitor_health"
STICKY_FILE = "stickies"


# Local JSON database lives in repo_root/database/*.json.
_DATABASE_DIR = Path(__file__).resolve().parent.parent / "database"
_PERSISTENCE_FILE_ALIASES = {
    "member_counter_configs": ("member_counter_config", "member_counter_configs"),
    "member_counter_config": ("member_counter_config", "member_counter_configs"),
    "boost_configs": ("boost_config", "boost_configs"),
    "boost_config": ("boost_config", "boost_configs"),
    "vouch_configs": ("vouch_config", "vouch_configs"),
    "vouch_config": ("vouch_config", "vouch_configs"),
    "onboarding_configs": ("onboarding_config", "onboarding_configs"),
    "onboarding_config": ("onboarding_config", "onboarding_configs"),
    "onboarding_verification_states": ("onboarding_verification_state", "onboarding_verification_states"),
    "onboarding_verification_state": ("onboarding_verification_state", "onboarding_verification_states"),
    "autoreplies": ("autoreply", "autoreplies"),
    "autoreply": ("autoreply", "autoreplies"),
    "open_tickets": ("tickets", "open_tickets"),
    "tickets": ("tickets", "open_tickets"),
    "stickies": ("sticky", "stickies"),
    "sticky": ("sticky", "stickies"),
    "fun_configs": ("fun_config", "fun_configs"),
    "fun_config": ("fun_config", "fun_configs"),
    "ai_help_global": ("ai_help_config", "ai_help_global"),
    "ai_help_config": ("ai_help_config", "ai_help_global"),
    "config_audit_log": ("config_audit_log", "config_audit_logs"),
    "config_audit_logs": ("config_audit_log", "config_audit_logs"),
}


_STATE_CACHE = {}
_JSON_SINGLETON_ID = 1

# Per-key timestamp of the last file mtime served from cache.
_STATE_CACHE_FILE_MTIME: dict[str, float] = {}
# Lock protecting cache state during concurrent reads/writes.
_CACHE_LOCK = threading.Lock()


def _normalize_persistence_key(path: str) -> str:
    key = str(path or "").strip().replace("\\", "/")
    if "/" in key:
        key = key.rsplit("/", 1)[-1]
    if key.endswith(".json"):
        key = key[:-5]
    return key


def _persistence_file_candidates(path: str) -> tuple[Path, ...]:
    key = _normalize_persistence_key(path)
    candidates = _PERSISTENCE_FILE_ALIASES.get(key, (key,))
    resolved: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_persistence_key(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        resolved.append(_DATABASE_DIR / f"{normalized}.json")
    return tuple(resolved)


def _read_json_document(file_path: Path, expected_type: type):
    try:
        with file_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        print(f"[Persistence] Failed to parse {file_path.name}: {exc}")
        return None
    except OSError as exc:
        print(f"[Persistence] Failed to read {file_path.name}: {exc}")
        return None

    if isinstance(raw, expected_type):
        return raw

    coerced = _coerce_stored_json(raw, expected_type)
    if coerced is not None:
        return coerced

    print(
        f"[Persistence] {file_path.name}: expected {expected_type.__name__}, got {type(raw).__name__}"
    )
    return None


def _write_json_document(file_path: Path, data) -> float:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=file_path.parent,
            prefix=f".{file_path.stem}.",
            suffix=".tmp",
        ) as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        os.replace(temp_name, file_path)
        return file_path.stat().st_mtime
    except Exception:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise

# Helpers shared by the local JSON persistence layer.


def json_storage_active() -> bool:
    """True when the local JSON database backend is available."""
    return True


def _rest_guild_id(value) -> object:
    """Prefer int snowflakes for PostgREST int8 columns; fall back to string."""
    s = str(value).strip()
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return s
    return s


def _is_snowflake_dict_key(key: object) -> bool:
    s = str(key).strip()
    return s.isdigit() and 17 <= len(s) <= 22


def _stickies_guild_is_channel_keyed(guild_data: dict) -> bool:
    """True when shape is ``{ channelSnowflake: { name?, title?, content?, ... }, ... }``."""
    if not isinstance(guild_data, dict):
        return False
    for k, v in guild_data.items():
        if str(k) in ("guild_id", "id"):
            continue
        if _is_snowflake_dict_key(k) and isinstance(v, dict):
            return True
    return False


def _normalize_stickies_storage(data: dict) -> dict:
    """
    Bot + dashboard use ``guild_id -> channel_id -> sticky fields``. Relational ``stickies``
    often stores one flat row per guild (channel_id + content + …); normalize on load.
    """
    if not isinstance(data, dict):
        return {}
    out = {}
    for gid, g in data.items():
        if not isinstance(g, dict):
            continue
        sg = str(gid)
        if _stickies_guild_is_channel_keyed(g):
            out[sg] = copy.deepcopy(g)
            continue
        if "channel_id" in g and "content" in g:
            cid = str(g.get("channel_id") or "").strip()
            if cid:
                inner = {
                    "name": g.get("name"),
                    "title": g.get("title"),
                    "content": g.get("content", ""),
                    "color": g.get("color"),
                    "last_message_id": g.get("last_message_id"),
                }
                if g.get("id") is not None:
                    inner["id"] = g.get("id")
                out[sg] = {cid: inner}
                continue
        out[sg] = copy.deepcopy(g)
    return out


def _stickies_guild_blob_to_flat_row(guild_data: dict, gid_rest: object) -> dict:
    """
    Persist channel-keyed guild stickies as one flat row.
    Extra channels beyond the first (sorted by id) are not stored in this shape.
    """
    if not isinstance(guild_data, dict):
        return {"guild_id": gid_rest, "channel_id": None, "content": ""}
    if _stickies_guild_is_channel_keyed(guild_data):
        pairs = []
        for k, v in guild_data.items():
            if str(k) in ("guild_id", "id"):
                continue
            if _is_snowflake_dict_key(k) and isinstance(v, dict):
                pairs.append((str(k), v))
        if not pairs:
            return {"guild_id": gid_rest, "channel_id": None, "content": ""}
        pairs.sort(key=lambda x: x[0])
        ch_id, body = pairs[0]
        row = {
            "guild_id": gid_rest,
            "channel_id": _rest_guild_id(ch_id) if ch_id.isdigit() else ch_id,
            "name": body.get("name"),
            "title": body.get("title"),
            "content": str(body.get("content", "")),
            "color": body.get("color"),
        }
        if body.get("last_message_id") is not None:
            row["last_message_id"] = body.get("last_message_id")
        if body.get("id") is not None:
            row["id"] = body.get("id")
        return row
    row = copy.deepcopy(guild_data)
    row["guild_id"] = gid_rest
    return row


def _null_empty_bigint_fields(table: str, row: dict) -> None:
    """In-place: empty string is invalid for Postgres bigint / int8 (22P02)."""
    if table == "announcement_drafts":
        optional_int8 = ("channel_id", "ping_role_id")
    elif table == "stickies":
        optional_int8 = ("channel_id",)
    elif table in ("poll_drafts", "giveaway_drafts", "select_menu_drafts", "polls"):
        optional_int8 = tuple(
            k
            for k in row.keys()
            if str(k).endswith("_id") and str(k) != "guild_id"
        )
    else:
        optional_int8 = ()
    for k in optional_int8:
        v = row.get(k)
        if v == "" or (isinstance(v, str) and not str(v).strip()):
            row[k] = None


def _coerce_stored_json(raw, expected_type: type):
    """
    Normalize JSON from PostgREST: usually dict/list, but some imports store JSON as text.
    """
    if raw is None:
        return None
    if isinstance(raw, expected_type):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, expected_type):
            return parsed
    return None


# ── Persistence (local JSON database) ───────────────────────────────────────


def _detach_state(obj):
    """
    Return a deep copy of JSON-like config so callers cannot mutate the in-process
    ``_STATE_CACHE`` (FastAPI handlers often filter or normalize in place — e.g. AI Help
    guild list scoped to the current user).
    """
    if obj is None:
        return None
    try:
        return json.loads(json.dumps(obj))
    except (TypeError, ValueError):
        return copy.deepcopy(obj)


def load_json(path: str, default=None):
    """
    Load document state from the local JSON database under ``database/*.json``.

    The logical key may differ from the on-disk filename; aliases are resolved
    automatically so existing data files continue to work.
    """
    if default is None:
        default = {}

    expected_type = type(default)
    candidates = _persistence_file_candidates(path)

    for file_path in candidates:
        cache_key = file_path.stem
        try:
            current_mtime = file_path.stat().st_mtime
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"[Persistence] Failed to stat {file_path.name}: {exc}")
            continue

        with _CACHE_LOCK:
            cached = _STATE_CACHE.get(cache_key)
            cached_mtime = _STATE_CACHE_FILE_MTIME.get(cache_key)

        if cached is not None and cached_mtime == current_mtime:
            data = cached
        else:
            data = _read_json_document(file_path, expected_type)
            if data is None:
                continue
            with _CACHE_LOCK:
                _STATE_CACHE[cache_key] = data
                _STATE_CACHE_FILE_MTIME[cache_key] = current_mtime

        if path == STICKY_FILE and isinstance(data, dict):
            data = _normalize_stickies_storage(data)
        return _detach_state(data)

    with _CACHE_LOCK:
        cached = None
        for file_path in candidates:
            cached = _STATE_CACHE.get(file_path.stem)
            if cached is not None:
                break

    if cached is not None:
        if path == STICKY_FILE and isinstance(cached, dict):
            cached = _normalize_stickies_storage(cached)
        return _detach_state(cached)

    return _detach_state(default) if isinstance(default, (dict, list)) else default


def save_json(path: str, data, *, warn_on_persist_failure: bool = True):
    """Persist state to the local JSON database."""
    snapshot = _detach_state(data)
    candidates = _persistence_file_candidates(path)
    if not candidates:
        return

    target_path = candidates[0]
    cache_key = target_path.stem

    try:
        current_mtime = _write_json_document(target_path, snapshot)
    except Exception as exc:
        if warn_on_persist_failure:
            print(f"[Persistence] Failed to save {path} to {target_path.name}: {exc}")
        invalidate_json_cache(path)
        return

    with _CACHE_LOCK:
        _STATE_CACHE[cache_key] = snapshot
        _STATE_CACHE_FILE_MTIME[cache_key] = current_mtime


def invalidate_json_cache(path: str | None = None) -> None:
    """Drop cached JSON for one path, or clear all cached documents."""
    if path is None:
        with _CACHE_LOCK:
            _STATE_CACHE.clear()
            _STATE_CACHE_FILE_MTIME.clear()
    else:
        with _CACHE_LOCK:
            for file_path in _persistence_file_candidates(path):
                cache_key = file_path.stem
                _STATE_CACHE.pop(cache_key, None)
                _STATE_CACHE_FILE_MTIME.pop(cache_key, None)


# ── Safe type helpers ─────────────────────────────────────────────────────────

def _as_int(value) -> int | None:
    """Convert *value* to int or return None."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value, fallback: int, minimum: int, maximum: int) -> int:
    """Parse *value* as int clamped to [minimum, maximum]."""
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


# ── Color helpers ─────────────────────────────────────────────────────────────

def parse_embed_color(raw: str | int | None, default_color: int = 0x5865F2) -> discord.Color:
    """Parse a hex-string or int into a ``discord.Color``."""
    try:
        if isinstance(raw, str) and raw.strip().startswith("#"):
            return discord.Color(int(raw.strip().lstrip("#"), 16))
        if raw not in (None, ""):
            return discord.Color(int(raw))
    except Exception:
        pass
    return discord.Color(default_color)


def parse_hex_color(value: str) -> int:
    """Convert '#RRGGBB' or 'RRGGBB' to an int, or return default green."""
    v = value.strip().lstrip("#")
    if len(v) == 6:
        try:
            return int(v, 16)
        except ValueError:
            pass
    return 5763719  # default green


def _color_to_rgba(
    color_value: str,
    opacity: int,
    default: tuple[int, int, int] = (255, 255, 255),
) -> tuple[int, int, int, int]:
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


# ── Interaction helpers ───────────────────────────────────────────────────────

async def send_ephemeral_error(interaction: discord.Interaction, message: str):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def is_server_owner(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and interaction.user.id == interaction.guild.owner_id)


def normalize_command_name(name: str) -> str:
    return " ".join(name.lower().split())


def normalize_discord_token(raw_token: str | None) -> str | None:
    """Normalize token text from env files/panels to avoid common formatting mistakes."""
    if raw_token is None:
        return None

    token = raw_token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        token = token[1:-1].strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()

    return token or None


# ── Env helpers ───────────────────────────────────────────────────────────────

def parse_id_set_from_env(var_name: str) -> set[int]:
    raw = os.getenv(var_name, "").strip()
    if not raw:
        return set()
    values = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            values.add(int(part))
    return values

