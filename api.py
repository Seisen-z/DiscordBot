from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, Body, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Dict, Any, Union
import asyncio
import time
import json
import os
import re
import mimetypes
import hashlib
import hmac
import uuid
import base64
import logging
import aiohttp
import certifi
import ssl
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from dotenv import load_dotenv
# NOTE: process_dashboard_trigger is imported locally inside each handler
# to prevent the circular import: api.py -> modules.ipc -> api.py

load_dotenv()

# Same persistence as the bot: local JSON files via modules.utils.
from modules.utils import load_json, save_json, json_storage_active, normalize_discord_token

_CONFIG_AUDIT_LOG_PATH = "config_audit_log"
_CONFIG_AUDIT_MAX_PER_GUILD = 300
_LOCAL_ASSET_ROOT = Path(__file__).resolve().parent / "database" / "assets"
# Short-lived cache for GET /users/@me (Bearer) used by config audit — avoids duplicate Discord calls on save bursts.
_actor_me_cache: Dict[str, tuple[dict, float]] = {}


def _json_safe_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except Exception:
        return value


def _merge_shallow_dict(base: Any, patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    Overlay ``patch`` onto dict ``base`` so omitted JSON keys from the dashboard do not
    wipe fields that were never sent (common cause of settings 'reverting' after save).
    """
    out: Dict[str, Any] = {}
    if isinstance(base, dict):
        out.update(base)
    if isinstance(patch, dict):
        out.update(patch)
    return out


def _merge_shallow_nested_guild(prev: Any, patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge a guild-scoped dict of nested dicts (e.g. announcement or poll drafts keyed by name).
    Each nested dict is shallow-merged so DB ``id`` and other columns survive partial saves.
    """
    out: Dict[str, Any] = dict(prev) if isinstance(prev, dict) else {}
    if not isinstance(patch, dict):
        return out
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def _diff_top_level_keys(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    changed: List[str] = []
    all_keys = sorted(set(before.keys()) | set(after.keys()))
    for key in all_keys:
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


async def _resolve_actor(request: Request) -> Dict[str, Any]:
    token = _bearer_token(request)
    cache_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    token_hash = cache_key[:10]
    try:
        actor_ttl = float(os.getenv("DISCORD_ACTOR_CACHE_TTL", "300").strip())
    except ValueError:
        actor_ttl = 300.0
    now = time.time()
    hit = _actor_me_cache.get(cache_key)
    if hit and now - hit[1] < actor_ttl:
        me = hit[0]
        return {
            "token_hash": token_hash,
            "actor_id": str(me.get("id") or "") or None,
            "actor_name": str(me.get("global_name") or me.get("username") or "") or None,
        }

    actor_id: Optional[str] = None
    actor_name: Optional[str] = None
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=_DASHBOARD_UA,
            connector=aiohttp.TCPConnector(ssl=_SOCIAL_SSL_CTX),
        ) as session:
            async with session.get(
                f"{DISCORD_API}/users/@me",
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status == 200:
                    me = await resp.json()
                    if isinstance(me, dict) and me.get("id"):
                        _actor_me_cache[cache_key] = (me, time.time())
                    if isinstance(me, dict):
                        actor_id = str(me.get("id") or "") or None
                        actor_name = str(me.get("global_name") or me.get("username") or "") or None
    except Exception:
        actor_id = None
        actor_name = None
    return {
        "token_hash": token_hash,
        "actor_id": actor_id,
        "actor_name": actor_name,
    }


async def _append_config_audit_entry(
    *,
    request: Request,
    guild_id: str,
    module: str,
    before: Any,
    after: Any,
) -> None:
    before_safe = _json_safe_copy(before)
    after_safe = _json_safe_copy(after)
    changed_keys: List[str] = []
    if isinstance(before_safe, dict) and isinstance(after_safe, dict):
        changed_keys = _diff_top_level_keys(before_safe, after_safe)

    actor = await _resolve_actor(request)
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "guild_id": str(guild_id),
        "module": str(module),
        "changed_keys": changed_keys,
        "before": stringify_ids(before_safe),
        "after": stringify_ids(after_safe),
        "actor_id": actor.get("actor_id"),
        "actor_name": actor.get("actor_name"),
        "actor_token_hash": actor.get("token_hash"),
    }

    root = load_json(_CONFIG_AUDIT_LOG_PATH, {})
    if not isinstance(root, dict):
        root = {}
    bucket = root.get(str(guild_id), [])
    if not isinstance(bucket, list):
        bucket = []
    bucket.append(entry)
    if len(bucket) > _CONFIG_AUDIT_MAX_PER_GUILD:
        bucket = bucket[-_CONFIG_AUDIT_MAX_PER_GUILD :]
    root[str(guild_id)] = bucket
    # Audit is best-effort; many deployments omit this table (and bot_state), so avoid CRITICAL spam.
    save_json(_CONFIG_AUDIT_LOG_PATH, root, warn_on_persist_failure=False)

app = FastAPI(title="Seisen Hub API")


def _parse_cors_origins() -> List[str]:
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://45.8.22.11:9460/",
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DISCORD_BOT_TOKEN = normalize_discord_token(os.getenv("DISCORD_TOKEN"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

DISCORD_API = "https://discord.com/api/v10"
_SOCIAL_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_YT_CHANNEL_ID_RE = re.compile(r"UC[a-zA-Z0-9_-]{22}")
_DISCORD_MSG_LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")

PERMISSION_ADMINISTRATOR = 1 << 3
PERMISSION_MANAGE_GUILD = 1 << 5
_DASHBOARD_GUILD_PATH_RE = re.compile(r"^/api/(?:bot/)?guilds/(\d+)")
_FUN_CONFIG_KEYS = frozenset(
    {
        "eight_ball",
        "roasts",
        "compliments",
        "jokes",
        "fun_facts",
        "riddles",
        "quotes",
        "hello_greetings",
    }
)
_DASHBOARD_UA = {"User-Agent": "SeisenHubDashboard/1.0"}


def _int_env(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return default
        return int(str(raw).strip(), 10)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return default
        return float(str(raw).strip())
    except ValueError:
        return default


# Discord @me/guilds (user token) — keep high to avoid 429 on dashboard bursts.
_MANAGEABLE_GUILDS_CACHE_TTL = _float_env("DISCORD_MANAGEABLE_GUILDS_CACHE_TTL", 300.0)
# When Discord 429s us, back off for this long before trying again for the same
# token instead of retrying on every incoming request — retrying immediately just
# keeps the token rate-limited indefinitely under any sustained traffic.
_MANAGEABLE_GUILDS_FAILURE_COOLDOWN = _float_env("DISCORD_MANAGEABLE_GUILDS_FAILURE_COOLDOWN", 30.0)
_user_manageable_guilds_cache: Dict[str, tuple[set[str], float]] = {}
_manageable_guilds_failure_until: Dict[str, float] = {}
_manageable_guilds_inflight: Dict[str, asyncio.Task] = {}
_manageable_guilds_sf_lock = asyncio.Lock()
# After OAuth, we only re-check @me periodically (not every dashboard call).
_ME_SESSION_TTL_SEC = _float_env("DISCORD_ME_SESSION_TTL_SEC", 600.0)
_me_session_ok_until: Dict[str, float] = {}
_AUTH_LOGGER = logging.getLogger("seisen.auth")
_PERSIST_LOGGER = logging.getLogger("seisen.persistence")


@app.on_event("startup")
async def _startup_check_storage() -> None:
    if not json_storage_active():
        _PERSIST_LOGGER.warning(
            "Local JSON database is unavailable — load_json returns empty defaults. "
            "Check that the database folder exists next to api.py."
        )


def _request_meta(request: Request) -> str:
    fwd = (request.headers.get("x-forwarded-for") or "").strip()
    ip = fwd.split(",")[0].strip() if fwd else None
    if not ip:
        client = getattr(request, "client", None)
        ip = getattr(client, "host", None) if client else None
    method = str(getattr(request, "method", "?"))
    path = str(getattr(getattr(request, "url", None), "path", "?"))
    return f"{method} {path} ip={ip or 'unknown'}"


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization") or ""
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        _AUTH_LOGGER.warning("auth_missing_bearer %s", _request_meta(request))
        raise HTTPException(status_code=401, detail="Authorization Bearer token required")
    tok = parts[1].strip()
    if not tok:
        _AUTH_LOGGER.warning("auth_empty_bearer %s", _request_meta(request))
        raise HTTPException(status_code=401, detail="Authorization Bearer token required")
    return tok


async def _fetch_discord_manageable_guild_ids_uncached(
    user_token: str, cache_key: str, request: Request | None
) -> set[str]:
    """One Discord GET /users/@me/guilds; raises HTTPException on auth/upstream errors."""
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=_DASHBOARD_UA,
        connector=aiohttp.TCPConnector(ssl=_SOCIAL_SSL_CTX),
    ) as session:
        async with session.get(
            f"{DISCORD_API}/users/@me/guilds",
            headers={"Authorization": f"Bearer {user_token}"},
        ) as resp:
            if resp.status != 200:
                if resp.status in {401, 403}:
                    _AUTH_LOGGER.warning(
                        "auth_discord_invalid_session discord_status=%s token=%s %s",
                        resp.status,
                        cache_key[:10],
                        _request_meta(request) if request is not None else "request=unknown",
                    )
                    raise HTTPException(status_code=401, detail="Discord session invalid or expired")
                if resp.status == 429:
                    _AUTH_LOGGER.warning(
                        "auth_discord_rate_limited discord_status=429 token=%s %s",
                        cache_key[:10],
                        _request_meta(request) if request is not None else "request=unknown",
                    )
                    raise HTTPException(
                        status_code=503,
                        detail="Discord auth check is rate-limited. Retry in a moment.",
                    )
                _AUTH_LOGGER.warning(
                    "auth_discord_upstream_error discord_status=%s token=%s %s",
                    resp.status,
                    cache_key[:10],
                    _request_meta(request) if request is not None else "request=unknown",
                )
                raise HTTPException(
                    status_code=502,
                    detail="Discord auth check failed temporarily. Retry in a moment.",
                )
            guilds = await resp.json()
    if not isinstance(guilds, list):
        raise HTTPException(status_code=403, detail="Could not read your Discord servers")
    out: set[str] = set()
    for g in guilds:
        gid = str(g.get("id") or "")
        if not gid:
            continue
        try:
            perms = int(g.get("permissions", "0"))
        except (TypeError, ValueError):
            perms = 0
        if perms & PERMISSION_ADMINISTRATOR or perms & PERMISSION_MANAGE_GUILD:
            out.add(gid)
    return out


async def _run_manageable_guild_ids_task(
    user_token: str, cache_key: str, request: Request | None
) -> set[str]:
    """Single-flight worker: one Discord call per token; many waiters share the same task."""
    try:
        out = await _fetch_discord_manageable_guild_ids_uncached(user_token, cache_key, request)
        _user_manageable_guilds_cache[cache_key] = (set(out), time.time())
        _manageable_guilds_failure_until.pop(cache_key, None)
        return out
    except HTTPException as exc:
        if exc.status_code in {429, 503}:
            # Discord is rate-limiting this token. Don't let the next request retry
            # immediately — that's what turns one 429 into a permanent outage under
            # any steady traffic. Serve the last known-good guild list (if any) and
            # hold off on hitting Discord again until the cooldown elapses.
            _manageable_guilds_failure_until[cache_key] = time.time() + _MANAGEABLE_GUILDS_FAILURE_COOLDOWN
            stale = _user_manageable_guilds_cache.get(cache_key)
            if stale is not None:
                if request is not None:
                    _AUTH_LOGGER.warning(
                        "auth_manageable_guilds_stale_fallback token=%s %s",
                        cache_key[:10],
                        _request_meta(request),
                    )
                return set(stale[0])
        raise
    finally:
        async with _manageable_guilds_sf_lock:
            _manageable_guilds_inflight.pop(cache_key, None)


async def _discord_user_manageable_guild_ids(user_token: str, *, request: Request | None = None) -> set[str]:
    """
    Guild IDs the user can manage (Manage Server / Admin). Cached and single-flight so a
    dashboard burst does not fan out dozens of identical Discord @me/guilds calls (429).
    """
    cache_key = hashlib.sha256(user_token.encode("utf-8")).hexdigest()
    now = time.time()
    cached = _user_manageable_guilds_cache.get(cache_key)
    if cached and now - cached[1] < _MANAGEABLE_GUILDS_CACHE_TTL:
        if request is not None:
            _AUTH_LOGGER.info(
                "auth_manageable_guilds_cache_hit token=%s %s",
                cache_key[:10],
                _request_meta(request),
            )
        return set(cached[0])

    # Still cooling down from a recent 429 — reuse stale data instead of retrying
    # Discord (which would just get 429'd again and reset the cooldown clock).
    failure_until = _manageable_guilds_failure_until.get(cache_key)
    if failure_until is not None and now < failure_until:
        if cached is not None:
            return set(cached[0])
        raise HTTPException(status_code=503, detail="Discord auth check is rate-limited. Retry in a moment.")

    async with _manageable_guilds_sf_lock:
        now = time.time()
        cached = _user_manageable_guilds_cache.get(cache_key)
        if cached and now - cached[1] < _MANAGEABLE_GUILDS_CACHE_TTL:
            if request is not None:
                _AUTH_LOGGER.info(
                    "auth_manageable_guilds_cache_hit token=%s %s",
                    cache_key[:10],
                    _request_meta(request),
                )
            return set(cached[0])

        failure_until = _manageable_guilds_failure_until.get(cache_key)
        if failure_until is not None and now < failure_until:
            if cached is not None:
                return set(cached[0])
            raise HTTPException(status_code=503, detail="Discord auth check is rate-limited. Retry in a moment.")

        if cache_key in _manageable_guilds_inflight:
            task = _manageable_guilds_inflight[cache_key]
        else:
            task = asyncio.create_task(
                _run_manageable_guild_ids_task(user_token, cache_key, request)
            )
            _manageable_guilds_inflight[cache_key] = task

    return await task


async def require_guild_dashboard_access(request: Request, guild_id: str) -> None:
    token = _bearer_token(request)
    gid = str(guild_id).strip()
    if not gid.isdigit():
        raise HTTPException(status_code=400, detail="Invalid guild id")
    manageable = await _discord_user_manageable_guild_ids(token, request=request)
    if gid not in manageable:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]
        _AUTH_LOGGER.warning(
            "auth_guild_forbidden guild=%s token=%s %s",
            gid,
            token_hash,
            _request_meta(request),
        )
        raise HTTPException(
            status_code=403,
            detail="Manage Server (or Administrator) in this server is required",
        )


async def require_guild_member_oauth(request: Request, guild_id: str) -> str:
    """Require Bearer token; user must be a member of guild_id. Returns Discord user snowflake."""
    token = _bearer_token(request)
    gid = str(guild_id).strip()
    if not gid.isdigit():
        raise HTTPException(status_code=400, detail="Invalid guild id")
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=_DASHBOARD_UA,
        connector=aiohttp.TCPConnector(ssl=_SOCIAL_SSL_CTX),
    ) as session:
        async with session.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=401, detail="Discord session invalid or expired")
            me = await resp.json()
        uid = str(me.get("id") or "")
        if not uid:
            raise HTTPException(status_code=401, detail="Discord session invalid")
        async with session.get(
            f"{DISCORD_API}/users/@me/guilds",
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=401, detail="Could not read your Discord servers")
            guilds = await resp.json()
    if not isinstance(guilds, list):
        raise HTTPException(status_code=403, detail="Could not verify guild membership")
    if gid not in {str(g.get("id")) for g in guilds}:
        raise HTTPException(status_code=403, detail="You must be a member of this server")
    return uid


async def require_authenticated_discord_user(request: Request) -> str:
    """Valid Bearer + Discord session. Cached a few minutes to avoid @me on every dashboard call."""
    token = _bearer_token(request)
    cache_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    if _me_session_ok_until.get(cache_key, 0.0) > now:
        return token

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=_DASHBOARD_UA,
        connector=aiohttp.TCPConnector(ssl=_SOCIAL_SSL_CTX),
    ) as session:
        async with session.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            if resp.status != 200:
                _me_session_ok_until.pop(cache_key, None)
                raise HTTPException(status_code=401, detail="Discord session invalid or expired")
            data = await resp.json()
            if not isinstance(data, dict) or not data.get("id"):
                _me_session_ok_until.pop(cache_key, None)
                raise HTTPException(status_code=401, detail="Discord session invalid")
    _me_session_ok_until[cache_key] = now + _ME_SESSION_TTL_SEC
    return token


# --- In-memory Discord API caches (declared before auth helpers that use them) ---
_GUILD_RESOURCE_CACHE_TTL = _float_env("DISCORD_GUILD_RESOURCE_CACHE_TTL", 300.0)
discord_channel_cache: Dict[str, Any] = {}
discord_role_cache: Dict[str, Any] = {}
discord_bot_guilds_cache = None  # (guild_ids_set, timestamp) — set at runtime
_guild_channel_fetch_locks: Dict[str, asyncio.Lock] = {}
_guild_roles_fetch_locks: Dict[str, asyncio.Lock] = {}
_discord_bot_guild_list_lock = asyncio.Lock()


def _per_guild_lock(guild_id: str, registry: Dict[str, asyncio.Lock]) -> asyncio.Lock:
    gid = str(guild_id).strip()
    lock = registry.get(gid)
    if lock is None:
        lock = asyncio.Lock()
        registry[gid] = lock
    return lock


async def _discord_bot_get_json_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict,
    max_attempts: int = 4,
) -> tuple[int, Any]:
    """GET with basic 429 Retry-After handling (Discord rate limits)."""
    last_status = 503
    last_body: Any = None
    for attempt in range(max_attempts):
        async with session.get(url, headers=headers) as resp:
            last_status = resp.status
            if resp.status == 429 and attempt + 1 < max_attempts:
                try:
                    await resp.read()
                except Exception:
                    pass
                ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                try:
                    wait = float(ra) if ra is not None else 1.0
                except ValueError:
                    wait = 1.0
                wait = min(max(wait, 0.25), 20.0)
                await asyncio.sleep(wait)
                continue
            try:
                last_body = await resp.json()
            except Exception:
                last_body = None
            return resp.status, last_body
    return last_status, last_body


async def _ensure_discord_guild_channels(guild_id: str) -> list:
    """Single-flight + cache for GET /guilds/{id}/channels (bot token)."""
    gid = str(guild_id).strip()
    now = time.time()
    cached = discord_channel_cache.get(gid)
    if cached and now - cached[1] < _GUILD_RESOURCE_CACHE_TTL:
        return cached[0]

    if not DISCORD_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Bot token not configured")

    lock = _per_guild_lock(gid, _guild_channel_fetch_locks)
    async with lock:
        now = time.time()
        cached = discord_channel_cache.get(gid)
        if cached and now - cached[1] < _GUILD_RESOURCE_CACHE_TTL:
            return cached[0]

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=_DASHBOARD_UA,
            connector=aiohttp.TCPConnector(ssl=_SOCIAL_SSL_CTX),
        ) as session:
            status, body = await _discord_bot_get_json_with_retry(
                session,
                f"{DISCORD_API}/guilds/{gid}/channels",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            )
            if status == 429:
                stale = discord_channel_cache.get(gid)
                if stale:
                    return stale[0]
                raise HTTPException(
                    status_code=503,
                    detail="Discord rate limited loading channels; retry shortly.",
                )
            if status != 200:
                raise HTTPException(
                    status_code=status,
                    detail=f"Discord API {status}: {body.get('message', body) if isinstance(body, dict) else body}",
                )
            if not isinstance(body, list):
                raise HTTPException(status_code=502, detail="Discord returned invalid channel list")

            channels = sorted(body, key=lambda c: (c.get("position", 0)))
            discord_channel_cache[gid] = (channels, time.time())
            return channels


async def _ensure_discord_guild_roles(guild_id: str) -> list:
    """Single-flight + cache for GET /guilds/{id}/roles (bot token)."""
    gid = str(guild_id).strip()
    now = time.time()
    cached = discord_role_cache.get(gid)
    if cached and now - cached[1] < _GUILD_RESOURCE_CACHE_TTL:
        return cached[0]

    if not DISCORD_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Bot token not configured")

    lock = _per_guild_lock(gid, _guild_roles_fetch_locks)
    async with lock:
        now = time.time()
        cached = discord_role_cache.get(gid)
        if cached and now - cached[1] < _GUILD_RESOURCE_CACHE_TTL:
            return cached[0]

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=_DASHBOARD_UA,
            connector=aiohttp.TCPConnector(ssl=_SOCIAL_SSL_CTX),
        ) as session:
            status, body = await _discord_bot_get_json_with_retry(
                session,
                f"{DISCORD_API}/guilds/{gid}/roles",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            )
            if status == 429:
                stale = discord_role_cache.get(gid)
                if stale:
                    return stale[0]
                raise HTTPException(
                    status_code=503,
                    detail="Discord rate limited loading roles; retry shortly.",
                )
            if status != 200:
                raise HTTPException(
                    status_code=status,
                    detail=f"Discord API {status}: {body.get('message', body) if isinstance(body, dict) else body}",
                )
            if not isinstance(body, list):
                raise HTTPException(status_code=502, detail="Discord returned invalid role list")

            roles = sorted(
                [r for r in body if r.get("name") != "@everyone"],
                key=lambda r: -r.get("position", 0),
            )
            discord_role_cache[gid] = (roles, time.time())
            return roles


async def _discord_bot_guild_ids_cached() -> set[str]:
    global discord_bot_guilds_cache
    now = time.time()
    if discord_bot_guilds_cache and now - discord_bot_guilds_cache[1] < _GUILD_RESOURCE_CACHE_TTL:
        return set(discord_bot_guilds_cache[0])

    if not DISCORD_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Bot token not configured")

    async with _discord_bot_guild_list_lock:
        now = time.time()
        if discord_bot_guilds_cache and now - discord_bot_guilds_cache[1] < _GUILD_RESOURCE_CACHE_TTL:
            return set(discord_bot_guilds_cache[0])

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=_DASHBOARD_UA,
            connector=aiohttp.TCPConnector(ssl=_SOCIAL_SSL_CTX),
        ) as session:
            status, body = await _discord_bot_get_json_with_retry(
                session,
                f"{DISCORD_API}/users/@me/guilds",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            )
            if status != 200:
                raise HTTPException(
                    status_code=status,
                    detail=f"Discord API error: {body.get('message', body) if isinstance(body, dict) else body}",
                )
            if not isinstance(body, list):
                raise HTTPException(status_code=502, detail="Discord returned invalid guild list")
            ids = {str(g["id"]) for g in body}
            discord_bot_guilds_cache = (ids, time.time())
            return set(ids)


def _guild_channel_snowflake_set(channels_json: Any) -> set[str]:
    out: set[str] = set()
    if not isinstance(channels_json, list):
        return out
    for ch in channels_json:
        if isinstance(ch, dict) and ch.get("id") is not None:
            out.add(str(ch["id"]))
    return out


async def _guild_channel_ids_for_autoreply(guild_id: str) -> set[str]:
    channels = await _ensure_discord_guild_channels(guild_id)
    return _guild_channel_snowflake_set(channels)


def _autoreply_rule_targets(rule: Dict[str, Any]) -> set[str]:
    targets = rule.get("targets") or []
    if not isinstance(targets, list):
        return set()
    out: set[str] = set()
    for t in targets:
        if isinstance(t, dict) and t.get("id") is not None:
            out.add(str(t["id"]).replace("category-", ""))
        elif t is not None:
            out.add(str(t).replace("category-", ""))
    return out


def _autoreply_rule_in_guild(rule: Dict[str, Any], channel_ids: set[str]) -> bool:
    return bool(_autoreply_rule_targets(rule) & channel_ids)


def _fun_default_payload() -> Dict[str, Any]:
    return {k: [] for k in _FUN_CONFIG_KEYS}


def _fun_get_guild_payload(guild_id: str) -> Dict[str, Any]:
    default = _fun_default_payload()
    root = load_json("fun_configs", {})
    if not isinstance(root, dict):
        return default
    guilds = root.get("guilds")
    if isinstance(guilds, dict):
        inner = guilds.get(str(guild_id))
        if isinstance(inner, dict):
            return {**default, **inner}
    legacy = {k: root[k] for k in _FUN_CONFIG_KEYS if k in root}
    if legacy:
        return {**default, **legacy}
    return default


def _fun_put_guild_payload(guild_id: str, payload: Dict[str, Any]) -> None:
    root = load_json("fun_configs", {})
    if not isinstance(root, dict):
        root = {}
    guilds = root.get("guilds")
    if not isinstance(guilds, dict):
        guilds = {}
    root["guilds"] = guilds
    merged = _fun_get_guild_payload(guild_id)
    for k in _FUN_CONFIG_KEYS:
        if k in payload:
            merged[k] = payload[k]
    root["guilds"][str(guild_id)] = {k: merged.get(k, []) for k in _FUN_CONFIG_KEYS}
    save_json("fun_configs", root)

AI_HELP_FALLBACK_MODELS: List[Dict[str, Any]] = [
    {"id": "openrouter/auto", "name": "OpenRouter Auto Router", "recommended": True},
    {"id": "meta-llama/llama-3.3-70b-instruct:free", "name": "Meta Llama 3.3 70B (Free)", "recommended": True},
    {"id": "google/gemini-2.5-flash-preview:free", "name": "Google Gemini 2.5 Flash Preview (Free)", "recommended": True},
    {"id": "mistralai/mistral-small-3.2-24b-instruct:free", "name": "Mistral Small 3.2 24B Instruct (Free)", "recommended": True},
    {"id": "deepseek/deepseek-chat-v3-0324:free", "name": "DeepSeek Chat V3 (Free)", "recommended": False},
]

AI_HELP_MODEL_ALIASES: Dict[str, str] = {
    "qwen/qwen3.6-plus-preview:free": "openrouter/auto",
    "google/gemini-2.0-flash-exp:free": "openrouter/auto",
    "nvidia/nemotron-3-super-120b-a12b:free": "openrouter/auto",
    "minimax/minimax-m2.5:free": "openrouter/auto",
}


def _normalize_ai_help_model_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return AI_HELP_MODEL_ALIASES.get(raw, raw)

# --- Models ---

class TriggerRequest(BaseModel):
    guild_id: str
    payload: Dict[str, Any] = {}

class AutoReplyTarget(BaseModel):
    id: int
    name: str
    is_category: bool

class AutoReplyButton(BaseModel):
    label: str
    url: str

class AutoReplyRule(BaseModel):
    id: Optional[int] = None
    name: Optional[str] = None
    keywords: List[str]
    targets: List[str]
    reply_message: Optional[str] = None
    embed_title: Optional[str] = None
    embed_description: Optional[str] = None
    embed_thumbnail: Optional[str] = None
    embed_footer: Optional[str] = None
    embed_color: Optional[int] = None
    buttons: List[AutoReplyButton] = Field(default_factory=list)
    delete_after: Optional[int] = None

    @field_validator("buttons", mode="before")
    @classmethod
    def _buttons_none_to_empty(cls, v):
        return [] if v is None else v

class AIHelpTarget(BaseModel):
    id: str
    name: str
    is_category: bool

class AIHelpGuildConfig(BaseModel):
    enabled: bool
    targets: List[AIHelpTarget]
    system_instructions: Optional[str] = None
    model: Optional[str] = None
    models: Optional[List[str]] = None

class AIHelpConfig(BaseModel):
    global_enabled: bool
    default_model: str
    available_models: Optional[List[str]] = None
    system_instructions: Optional[str] = None
    guilds: Dict[str, AIHelpGuildConfig]

class AnnouncementDraft(BaseModel):
    title: str
    description: str
    content: Optional[str] = None
    thumbnail_url: Optional[str] = None
    image_url: Optional[str] = None
    images: List[str] = Field(default_factory=list)
    footer: Optional[str] = None
    channel_id: Optional[str] = None
    ping_role_id: Optional[str] = None
    buttons: List[Dict[str, str]] = Field(default_factory=list)

    @field_validator("channel_id", "ping_role_id", mode="before")
    @classmethod
    def _snowflake_optional(cls, v: Any) -> Optional[str]:
        # Dashboard placeholders send ""; Postgres bigint rejects "" (22P02).
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        return s

class TicketsConfigGuild(BaseModel):
    panel_channel_id: Optional[str] = None
    panel_message_id: Optional[str] = None
    ticket_category_id: Optional[str] = None
    support_role_ids: List[str] = []
    open_tickets: Dict[str, Any] = {}
    embed_title: Optional[str] = "🎫 Support Tickets"
    embed_description: str = "Do you need help or have a question? Click the button below to open a private ticket with our support team."
    embed_color: Optional[str] = "#5865F2"
    embed_thumbnail: Optional[str] = None
    embed_footer: Optional[str] = "Seisen Support System"

class KeyPanelConfig(BaseModel):
    channel_id: str
    message_id: Optional[str] = None
    title: str
    description: str
    button_label: str = "Generate Key"
    required_role_ids: List[str] = []
    webhook_url: Optional[str] = None
    webhook_secret: Optional[str] = None
    webhook_hmac_header: Optional[str] = None
    product_name: Optional[str] = None
    embed_color: Optional[str] = None
    embed_thumbnail: Optional[str] = None
    embed_image: Optional[str] = None
    embed_footer: Optional[str] = None

class RobloxMonitor(BaseModel):
    name: Optional[str] = None
    universe_id: Optional[str] = None
    channel_id: Optional[str] = None
    role_id: Optional[str] = None
    last_updated: Optional[str] = None

class SocialMonitor(BaseModel):
    name: Optional[str] = None
    platform: str = "rss"
    source: str
    channel_id: Optional[str] = None
    role_id: Optional[str] = None
    enabled: bool = True
    last_entry_id: Optional[str] = None
    last_checked_at: Optional[str] = None
    last_posted_at: Optional[str] = None
    last_error: Optional[str] = None

class StickyConfig(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    content: str
    color: Optional[int] = None

class BoostConfigGuild(BaseModel):
    category_id: Optional[str] = None
    channel_id: Optional[str] = None
    roles: List[str] = []

class MemberCounterConfigGuild(BaseModel):
    enabled: bool = False
    channel_id: Optional[str] = None
    channel_type: str = "voice"
    category_id: Optional[str] = None
    prefix: str = "Members: "
    include_bots: bool = False
    last_count: Optional[int] = 0
    last_updated: Optional[str] = None
    created_at: Optional[str] = None
    created_by: Optional[str] = None

class OnboardingConfigGuild(BaseModel):
    enabled: bool = False
    verified_role_id: Optional[str] = None
    auto_role_ids: List[str] = []

    welcome_enabled: bool = True
    welcome_channel_id: Optional[str] = None
    welcome_content: Optional[str] = ""
    welcome_embed_title: Optional[str] = "Welcome ${userglobalnickname}!"
    welcome_embed_description: Optional[str] = "to ${guildname}\n\nYou are member #${guildmembercount}."
    welcome_embed_color: Optional[str] = "#5865F2"
    welcome_embed_thumbnail: Optional[str] = None
    welcome_embed_image: Optional[str] = None
    welcome_embed_footer: Optional[str] = "Enjoy your stay"

    # Sapphire-style welcome builder collections.
    welcome_message_groups: List[Dict[str, Any]] = []
    welcome_messages: List[Dict[str, Any]] = []
    welcome_dynamic_images: List[Dict[str, Any]] = []

    send_welcome_on_join: bool = False

    join_guard_enabled: bool = False
    min_account_age_days: int = 0
    block_default_avatar: bool = False
    join_guard_action: str = "kick"
    join_guard_log_channel_id: Optional[str] = None

# --- Helpers ---

def stringify_ids(obj: Any) -> Any:
    """Recursively convert any integer that looks like a Discord Snowflake (>= 17 digits) to a string.
    This prevents JavaScript from silently corrupting large IDs due to floating-point precision limits."""
    if isinstance(obj, dict):
        return {k: stringify_ids(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [stringify_ids(i) for i in obj]
    if isinstance(obj, int) and obj > 9_999_999_999_999_999:  # 16+ digit number = snowflake
        return str(obj)
    return obj


def _coerce_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    return fallback


def _coerce_int(value: Any, fallback: int, minimum: int | None = None) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = fallback
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _coerce_str(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value)
    return text if text != "" else fallback


def _coerce_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _sanitize_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_value(v) for v in value]
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
    return value


def _coerce_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for entry in value:
        if entry in (None, ""):
            continue
        out.append(str(entry))
    return out


def _coerce_object_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        sanitized = _sanitize_json_value(entry)
        if isinstance(sanitized, dict):
            out.append(stringify_ids(sanitized))
    return out


def _coerce_onboarding_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    defaults = OnboardingConfigGuild().model_dump()
    sanitized_raw = _sanitize_json_value(raw if isinstance(raw, dict) else {})
    merged: Dict[str, Any] = {**defaults, **(sanitized_raw if isinstance(sanitized_raw, dict) else {})}

    bool_fields = [
        "enabled",
        "welcome_enabled",
        "send_welcome_on_join",
        "join_guard_enabled",
        "block_default_avatar",
    ]
    for key in bool_fields:
        merged[key] = _coerce_bool(merged.get(key), bool(defaults.get(key, False)))

    merged["min_account_age_days"] = _coerce_int(merged.get("min_account_age_days"), int(defaults.get("min_account_age_days", 0)), minimum=0)
    merged["join_guard_action"] = "ban" if str(merged.get("join_guard_action") or "").strip().lower() == "ban" else "kick"

    optional_string_fields = [
        "verified_role_id",
        "welcome_channel_id",
        "welcome_content",
        "welcome_embed_title",
        "welcome_embed_description",
        "welcome_embed_color",
        "welcome_embed_thumbnail",
        "welcome_embed_image",
        "welcome_embed_footer",
        "join_guard_log_channel_id",
    ]
    for key in optional_string_fields:
        merged[key] = _coerce_optional_str(merged.get(key))

    merged["auto_role_ids"] = _coerce_str_list(merged.get("auto_role_ids"))
    merged["welcome_message_groups"] = _coerce_object_list(merged.get("welcome_message_groups"))
    merged["welcome_messages"] = _coerce_object_list(merged.get("welcome_messages"))
    merged["welcome_dynamic_images"] = _coerce_object_list(merged.get("welcome_dynamic_images"))

    return stringify_ids(merged)

def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _social_test_error_detail(value: Any, max_len: int = 320) -> str:
    message = str(value or "").strip()
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


def _social_strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _social_entry_text(entry: ET.Element, names: set[str]) -> Optional[str]:
    for node in entry.iter():
        if _social_strip_ns(node.tag) in names and node.text and node.text.strip():
            return node.text.strip()
    return None


def _social_entry_author(entry: ET.Element) -> Optional[str]:
    for node in entry.iter():
        local = _social_strip_ns(node.tag)
        if local == "creator" and node.text and node.text.strip():
            return node.text.strip()
        if local == "author":
            if node.text and node.text.strip():
                return node.text.strip()
            for child in node.iter():
                if _social_strip_ns(child.tag) == "name" and child.text and child.text.strip():
                    return child.text.strip()
    return None


def _social_entry_link(entry: ET.Element) -> Optional[str]:
    for node in entry.iter():
        local = _social_strip_ns(node.tag)
        if local == "link":
            href = (node.attrib.get("href") or "").strip()
            if href:
                return href
            if node.text and node.text.strip().startswith("http"):
                return node.text.strip()
        if local in {"url", "origlink"} and node.text and node.text.strip().startswith("http"):
            return node.text.strip()
    return None


def _social_entry_thumbnail(entry: ET.Element) -> Optional[str]:
    for node in entry.iter():
        local = _social_strip_ns(node.tag)
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


def _social_extract_youtube_video_url(entry_id: Optional[str]) -> Optional[str]:
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


def _social_parse_feed_time(value: Optional[str]) -> Optional[datetime]:
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


def _social_extract_youtube_channel_id_from_html(html: str) -> Optional[str]:
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


async def _social_resolve_youtube_channel_id(source: str, session: aiohttp.ClientSession) -> Optional[str]:
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
            ssl=_SOCIAL_SSL_CTX,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status >= 400:
                return None
            html = await resp.text()
    except Exception:
        return None

    return _social_extract_youtube_channel_id_from_html(html)


def _social_normalize_youtube_source_url(source: str) -> Optional[str]:
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


def _social_build_youtube_tracking_url(source: str, channel_id: Optional[str]) -> Optional[str]:
    if channel_id:
        return f"https://www.youtube.com/channel/{channel_id}/videos"

    normalized = _social_normalize_youtube_source_url(source)
    if not normalized:
        return None

    lowered = normalized.lower().rstrip("/")
    if lowered.endswith("/videos"):
        return normalized
    return f"{normalized.rstrip('/')}/videos"


async def _social_fetch_latest_youtube_entry(source: str, session: aiohttp.ClientSession) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Fetch the latest video from a YouTube channel using the public Atom RSS feed."""
    src = (source or "").strip()
    if not src:
        raise RuntimeError("Missing YouTube source URL or channel id.")

    channel_id = await _social_resolve_youtube_channel_id(src, session)
    if not channel_id:
        raise RuntimeError(
            "Could not resolve a YouTube Channel ID from Source. "
            "Use a channel URL, @handle, or UC... channel ID."
        )

    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

    async with session.get(
        feed_url,
        ssl=_SOCIAL_SSL_CTX,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"YouTube RSS feed request failed with status {resp.status}")
        xml_text = await resp.text()

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise RuntimeError(f"YouTube RSS feed parse failed: {e}")

    entries = [node for node in root.iter() if _social_strip_ns(node.tag) == "entry"]
    if not entries:
        raise RuntimeError("YouTube RSS feed contained no video entries.")

    entry = entries[0]
    video_id = _social_entry_text(entry, {"videoid"})
    entry_id_raw = _social_entry_text(entry, {"id"})
    title = _social_entry_text(entry, {"title"}) or "New YouTube upload"
    published_raw = _social_entry_text(entry, {"published", "updated"})
    link = _social_entry_link(entry)

    # Fall back to parsing <id>yt:video:XXXXXXXXXXX</id>
    if not video_id and entry_id_raw:
        video_id = entry_id_raw.rsplit(":", 1)[-1]

    if not link and video_id:
        link = f"https://www.youtube.com/watch?v={video_id}"

    if not video_id and not link:
        raise RuntimeError("Could not extract video ID from YouTube RSS feed entry.")

    stable_id = video_id or link
    thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else None
    tracking_url = f"https://www.youtube.com/channel/{channel_id}/videos"

    return tracking_url, {
        "id": stable_id,
        "title": title,
        "author": source,
        "link": link,
        "thumbnail": thumbnail,
        "published_raw": published_raw,
        "published_at": _social_parse_feed_time(published_raw),
    }


def _social_resolve_source_url(
    platform: str,
    source: str,
) -> Optional[str]:
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


async def _social_fetch_latest_source_entry(source_url: str, session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
    async with session.get(
        source_url,
        ssl=_SOCIAL_SSL_CTX,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Source request failed with status {resp.status}")
        xml_text = await resp.text()

    probe = xml_text[:500].lower()
    if "<html" in probe and "<rss" not in probe and "<feed" not in probe:
        raise RuntimeError(
            "Source URL returned HTML instead of RSS/Atom XML. "
            "For YouTube, use platform=YouTube and set Source to the channel URL/@handle/UC... ID."
        )

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise RuntimeError(f"Feed parse failed: {e}")

    entries = [node for node in root.iter() if _social_strip_ns(node.tag) in {"entry", "item"}]
    if not entries:
        return None

    entry = entries[0]
    entry_id = _social_entry_text(entry, {"id", "guid", "videoid"})
    link = _social_entry_link(entry) or _social_extract_youtube_video_url(entry_id)
    title = _social_entry_text(entry, {"title"}) or "New post"
    author = _social_entry_author(entry)
    published_raw = _social_entry_text(entry, {"published", "updated", "pubdate", "date"})
    published_at = _social_parse_feed_time(published_raw)
    thumbnail = _social_entry_thumbnail(entry)

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


def _snowflake_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return str(parsed)


def _normalize_poll_entry(raw: Dict[str, Any], *, guild_id: str, message_id: str) -> Dict[str, Any]:
    options: List[str] = []
    for option in raw.get("options", []):
        text = str(option or "").strip()
        if not text:
            continue
        cleaned = text[:55]
        if cleaned in options:
            continue
        options.append(cleaned)
        if len(options) == 10:
            break

    duration_hours = _coerce_int(raw.get("duration_hours"), 24, minimum=1)
    duration_hours = min(168, duration_hours)

    return {
        "guild_id": _snowflake_str(raw.get("guild_id")) or _snowflake_str(guild_id) or str(guild_id),
        "channel_id": _snowflake_str(raw.get("channel_id")),
        "message_id": _snowflake_str(raw.get("message_id")) or _snowflake_str(message_id) or str(message_id),
        "created_by": _snowflake_str(raw.get("created_by")),
        "question": str(raw.get("question") or "Untitled Poll"),
        "options": options,
        "duration_hours": duration_hours,
        "multiple": _coerce_bool(raw.get("multiple"), False),
        "created_at": _coerce_optional_str(raw.get("created_at")),
        "end_at": _coerce_optional_str(raw.get("end_at")),
        "ended": _coerce_bool(raw.get("ended"), False),
        "ended_at": _coerce_optional_str(raw.get("ended_at")),
        "ended_by": _snowflake_str(raw.get("ended_by")),
        "ended_reason": _coerce_optional_str(raw.get("ended_reason")),
        "jump_url": _coerce_optional_str(raw.get("jump_url")),
    }


def _normalize_giveaway_entry(raw: Dict[str, Any], *, guild_id: str, message_id: str) -> Dict[str, Any]:
    winners: List[str] = []
    for user_id in raw.get("winners", []):
        parsed = _snowflake_str(user_id)
        if parsed and parsed not in winners:
            winners.append(parsed)

    reroll_winners: List[str] = []
    for user_id in raw.get("last_reroll_winners", []):
        parsed = _snowflake_str(user_id)
        if parsed and parsed not in reroll_winners:
            reroll_winners.append(parsed)

    awarded_user_ids: List[str] = []
    for user_id in raw.get("awarded_user_ids", []):
        parsed = _snowflake_str(user_id)
        if parsed and parsed not in awarded_user_ids:
            awarded_user_ids.append(parsed)

    key_tier = str(raw.get("key_tier") or "none").strip().lower()
    if key_tier not in {"none", "weekly", "monthly", "lifetime"}:
        key_tier = "none"

    key_delivery_raw = raw.get("key_delivery") if isinstance(raw.get("key_delivery"), dict) else {}
    key_delivery_failed_user_ids: List[str] = []
    for user_id in key_delivery_raw.get("failed_user_ids", []):
        parsed = _snowflake_str(user_id)
        if parsed and parsed not in key_delivery_failed_user_ids:
            key_delivery_failed_user_ids.append(parsed)

    key_delivery: Optional[Dict[str, Any]] = None
    if key_tier != "none":
        key_delivery = {
            "tier": key_tier,
            "delivered_count": _coerce_int(key_delivery_raw.get("delivered_count"), 0, minimum=0),
            "failed_count": _coerce_int(key_delivery_raw.get("failed_count"), 0, minimum=0),
            "failed_user_ids": key_delivery_failed_user_ids,
            "last_delivery_at": _coerce_optional_str(key_delivery_raw.get("last_delivery_at")),
        }

    winner_count = _coerce_int(raw.get("winner_count"), 1, minimum=1)
    winner_count = min(25, winner_count)
    duration_minutes = _coerce_int(raw.get("duration_minutes"), 60, minimum=1)
    duration_minutes = min(10080, duration_minutes)

    return {
        "guild_id": _snowflake_str(raw.get("guild_id")) or _snowflake_str(guild_id) or str(guild_id),
        "channel_id": _snowflake_str(raw.get("channel_id")),
        "message_id": _snowflake_str(raw.get("message_id")) or _snowflake_str(message_id) or str(message_id),
        "host_id": _snowflake_str(raw.get("host_id")),
        "reward_title": str(raw.get("reward_title") or "Mystery Reward"),
        "reward_description": str(raw.get("reward_description") or ""),
        "winner_count": winner_count,
        "duration_minutes": duration_minutes,
        "ping_role_id": _snowflake_str(raw.get("ping_role_id")),
        "key_log_channel_id": _snowflake_str(raw.get("key_log_channel_id")),
        "emoji": str(raw.get("emoji") or "🎉"),
        "created_at": _coerce_optional_str(raw.get("created_at")),
        "end_at": _coerce_optional_str(raw.get("end_at")),
        "ended": _coerce_bool(raw.get("ended"), False),
        "ended_at": _coerce_optional_str(raw.get("ended_at")),
        "ended_by": _snowflake_str(raw.get("ended_by")),
        "ended_reason": _coerce_optional_str(raw.get("ended_reason")),
        "entry_count": _coerce_int(raw.get("entry_count"), 0, minimum=0),
        "winners": winners,
        "jump_url": _coerce_optional_str(raw.get("jump_url")),
        "reroll_count": _coerce_int(raw.get("reroll_count"), 0, minimum=0),
        "last_rerolled_at": _coerce_optional_str(raw.get("last_rerolled_at")),
        "last_rerolled_by": _snowflake_str(raw.get("last_rerolled_by")),
        "last_reroll_winners": reroll_winners,
        "key_tier": key_tier,
        "key_delivery": key_delivery,
        "awarded_user_ids": awarded_user_ids,
    }

# --- Routes ---

# Bot Guilds (intersection: servers you manage ∩ servers the bot is in)
@app.get("/api/bot/guilds")
@app.get("/api/guilds")
async def get_bot_guilds(request: Request):
    user_token = _bearer_token(request)
    try:
        manageable = await _discord_user_manageable_guild_ids(user_token)
    except HTTPException as exc:
        # Discord rate-limited the guild lookup — return empty so the
        # dashboard still loads; the frontend degrades gracefully.
        if exc.status_code in {429, 503}:
            return {"guild_ids": [], "rate_limited": True}
        raise
    try:
        bot_ids = await _discord_bot_guild_ids_cached()
    except HTTPException:
        bot_ids = set()
    return {"guild_ids": sorted(manageable & bot_ids)}


@app.get("/api/guilds/{guild_id}/config_audit")
@app.get("/api/bot/guilds/{guild_id}/config_audit")
async def get_config_audit(guild_id: str, limit: int = 50):
    root = load_json(_CONFIG_AUDIT_LOG_PATH, {})
    if not isinstance(root, dict):
        root = {}
    items = root.get(str(guild_id), [])
    if not isinstance(items, list):
        items = []
    safe_limit = max(1, min(200, int(limit)))
    return {"items": stringify_ids(items[-safe_limit:][::-1])}

# Guild Channels (fetched live from Discord)
@app.get("/api/guilds/{guild_id}/channels")
async def get_guild_channels(guild_id: str):
    """Fetch all channels for a guild using the bot token (cached; single-flight per guild)."""
    return await _ensure_discord_guild_channels(guild_id)


# Guild Roles (fetched live from Discord)
@app.get("/api/guilds/{guild_id}/roles")
async def get_guild_roles(guild_id: str):
    """Fetch all roles for a guild using the bot token (cached; single-flight per guild)."""
    return await _ensure_discord_guild_roles(guild_id)


def _ai_help_normalize_storage_inplace(data: Dict[str, Any]) -> bool:
    changed = False
    fallback_ids = [m["id"] for m in AI_HELP_FALLBACK_MODELS]

    if "global_enabled" not in data:
        data["global_enabled"] = True
        changed = True

    if "system_instructions" not in data:
        data["system_instructions"] = ""
        changed = True

    if "guilds" not in data or not isinstance(data["guilds"], dict):
        data["guilds"] = {}
        changed = True

    available_models = data.get("available_models")
    normalized_available: List[str] = []
    seen_available: set[str] = set()
    if isinstance(available_models, list):
        for item in available_models:
            normalized = _normalize_ai_help_model_id(item)
            if normalized and normalized not in seen_available:
                seen_available.add(normalized)
                normalized_available.append(normalized)
    if not normalized_available:
        normalized_available = fallback_ids
    if available_models != normalized_available:
        data["available_models"] = normalized_available
        changed = True

    normalized_default = _normalize_ai_help_model_id(data.get("default_model")) or "openrouter/auto"
    if data.get("default_model") != normalized_default:
        data["default_model"] = normalized_default
        changed = True

    guilds = data.get("guilds")
    if isinstance(guilds, dict):
        for guild_cfg in guilds.values():
            if not isinstance(guild_cfg, dict):
                continue
            
            if "enabled" not in guild_cfg:
                guild_cfg["enabled"] = False
                changed = True
            
            if "targets" not in guild_cfg or not isinstance(guild_cfg["targets"], list):
                guild_cfg["targets"] = []
                changed = True
            
            if "system_instructions" not in guild_cfg:
                guild_cfg["system_instructions"] = ""
                changed = True

            # Guild UI reads per-guild instructions; relational DB often stores only global text.
            if not str(guild_cfg.get("system_instructions", "")).strip():
                glob_instr = data.get("system_instructions")
                if isinstance(glob_instr, str) and glob_instr.strip():
                    guild_cfg["system_instructions"] = glob_instr
                    changed = True
                elif glob_instr not in (None, ""):
                    guild_cfg["system_instructions"] = str(glob_instr)
                    changed = True

            normalized_model = _normalize_ai_help_model_id(guild_cfg.get("model"))
            if guild_cfg.get("model") != normalized_model and guild_cfg.get("model") is not None:
                guild_cfg["model"] = normalized_model or None
                changed = True

            guild_models = guild_cfg.get("models")
            if isinstance(guild_models, list):
                normalized_models: List[str] = []
                seen_models: set[str] = set()
                for item in guild_models:
                    normalized_item = _normalize_ai_help_model_id(item)
                    if normalized_item and normalized_item not in seen_models:
                        seen_models.add(normalized_item)
                        normalized_models.append(normalized_item)
                if normalized_models != guild_models:
                    guild_cfg["models"] = normalized_models
                    changed = True

            # Dashboard multi-select uses ``models``; slash commands / legacy rows may only set ``model``.
            if not isinstance(guild_cfg.get("models"), list) or not guild_cfg.get("models"):
                single = guild_cfg.get("model")
                if single:
                    nm = _normalize_ai_help_model_id(single)
                    if nm:
                        guild_cfg["models"] = [nm]
                        changed = True

    return changed


def _sync_ai_help_globals_from_guilds(stored: Dict[str, Any]) -> None:
    """
    Keep root ``system_instructions`` and ``available_models`` aligned with per-guild
    settings so relational ``ai_help_global`` reflects dashboard edits (guild-only
    payloads used to leave ``available_models`` stale.
    """
    guilds = stored.get("guilds")
    if not isinstance(guilds, dict):
        return

    best_instr = str(stored.get("system_instructions") or "")
    for gcfg in guilds.values():
        if isinstance(gcfg, dict):
            t = str(gcfg.get("system_instructions") or "")
            if len(t) > len(best_instr):
                best_instr = t
    if best_instr.strip():
        stored["system_instructions"] = best_instr

    avail: List[str] = []
    if isinstance(stored.get("available_models"), list):
        avail = [str(x).strip() for x in stored["available_models"] if str(x).strip()]
    seen = set(avail)
    for gcfg in guilds.values():
        if not isinstance(gcfg, dict):
            continue
        for mid in gcfg.get("models") or []:
            s = _normalize_ai_help_model_id(mid)
            if s and s not in seen:
                seen.add(s)
                avail.append(s)
        one = _normalize_ai_help_model_id(gcfg.get("model"))
        if one and one not in seen:
            seen.add(one)
            avail.append(one)
    if avail:
        stored["available_models"] = avail


# Debug endpoint (disabled unless ENABLE_DEBUG_API=1 — never exposes token material)
@app.get("/api/debug")
async def debug_info():
    flag = os.getenv("ENABLE_DEBUG_API", "").strip().lower()
    if flag not in {"1", "true", "yes"}:
        raise HTTPException(status_code=404, detail="Not found")
    return {"token_loaded": bool(DISCORD_BOT_TOKEN)}

@app.get("/api/debug_db")
async def debug_db():
    return {
        "json_storage_active": json_storage_active(),
        "database_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), "database")),
        "db_autoreplies_type": str(type(load_json("autoreplies", []))),
        "db_automod_type": str(type(load_json("automod_configs", {}))),
        "db_automod_keys": list(load_json("automod_configs", {}).keys()),
        "cwd": os.getcwd()
    }


@app.get("/api/guilds/{guild_id}/autoreply")
async def get_autoreply(guild_id: str):
    channel_ids = await _guild_channel_ids_for_autoreply(guild_id)
    data = load_json("autoreplies", [])
    if not isinstance(data, list):
        return []
    filtered = [r for r in data if isinstance(r, dict) and _autoreply_rule_in_guild(r, channel_ids)]
    return stringify_ids(filtered)


@app.put("/api/guilds/{guild_id}/autoreply")
async def update_autoreply(guild_id: str, rules: List[AutoReplyRule], request: Request):
    channel_ids = await _guild_channel_ids_for_autoreply(guild_id)
    full = load_json("autoreplies", [])
    if not isinstance(full, list):
        full = []
    before_rules = [r for r in full if isinstance(r, dict) and _autoreply_rule_in_guild(r, channel_ids)]
    new_rules = []
    for r in rules:
        d = r.model_dump()
        if d.get("id") is None:
            d.pop("id", None)
        new_rules.append(d)
    
    rest = [r for r in full if isinstance(r, dict) and not _autoreply_rule_in_guild(r, channel_ids)]
    save_json("autoreplies", rest + new_rules)

    await _append_config_audit_entry(
        request=request,
        guild_id=guild_id,
        module="autoreply",
        before={"rules": before_rules},
        after={"rules": new_rules},
    )
    return {"status": "success"}

# AI Help
@app.get("/api/ai_help")
async def get_ai_help(request: Request):
    user_token = await require_authenticated_discord_user(request)
    manageable = await _discord_user_manageable_guild_ids(user_token)
    bot_ids = await _discord_bot_guild_ids_cached()
    allowed = manageable & bot_ids

    data = load_json("ai_help_global", {})
    if not isinstance(data, dict):
        data = {}
    changed = _ai_help_normalize_storage_inplace(data)
    guilds = data.get("guilds")
    if isinstance(guilds, dict):
        data["guilds"] = {k: v for k, v in guilds.items() if str(k) in allowed}
    if changed:
        save_json("ai_help_global", data)
    return stringify_ids(data)


@app.put("/api/ai_help")
async def update_ai_help(request: Request, config: AIHelpConfig):
    user_token = await require_authenticated_discord_user(request)
    manageable = await _discord_user_manageable_guild_ids(user_token)
    bot_ids = await _discord_bot_guild_ids_cached()
    allowed = manageable & bot_ids

    stored = load_json("ai_help_global", {})
    if not isinstance(stored, dict):
        stored = {}
    _ai_help_normalize_storage_inplace(stored)

    incoming = config.model_dump()
    stored_guilds = stored.get("guilds")
    if not isinstance(stored_guilds, dict):
        stored_guilds = {}
    inc_guilds = incoming.get("guilds") or {}
    if not isinstance(inc_guilds, dict):
        inc_guilds = {}

    for gid, gcfg in inc_guilds.items():
        sgid = str(gid)
        if sgid in allowed and isinstance(gcfg, dict):
            prev = stored_guilds.get(sgid) if isinstance(stored_guilds.get(sgid), dict) else {}
            stored_guilds[sgid] = _merge_shallow_dict(prev, gcfg)

    stored["guilds"] = stored_guilds
    managed_stored_keys = {str(k) for k in stored_guilds.keys()}
    may_edit_globals = (not managed_stored_keys) or managed_stored_keys.issubset(allowed)

    if may_edit_globals:
        for key in ("global_enabled", "default_model", "available_models", "system_instructions"):
            if key in incoming:
                stored[key] = incoming[key]

    _sync_ai_help_globals_from_guilds(stored)

    _ai_help_normalize_storage_inplace(stored)
    save_json("ai_help_global", stored)
    return {"status": "success"}

@app.get("/api/ai_help/models")
async def get_available_models(request: Request):
    """Get list of available AI models."""
    await require_authenticated_discord_user(request)
    default_model = "openrouter/auto"

    if not OPENROUTER_API_KEY:
        return {
            "models": AI_HELP_FALLBACK_MODELS,
            "default": default_model,
        }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://discord.com",
        "X-Title": "Seisen AI Help",
    }

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers=headers,
        ) as session:
            async with session.get("https://openrouter.ai/api/v1/models") as resp:
                if resp.status >= 400:
                    raise HTTPException(status_code=resp.status, detail="Failed to fetch model catalog.")
                payload = await resp.json(content_type=None)
    except Exception:
        return {
            "models": AI_HELP_FALLBACK_MODELS,
            "default": default_model,
        }

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {
            "models": AI_HELP_FALLBACK_MODELS,
            "default": default_model,
        }

    blocked_terms = {
        "embedding",
        "moderation",
        "transcription",
        "rerank",
        "tts",
        "whisper",
        "speech",
    }

    models: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or "").strip()
        if not model_id or model_id in seen_ids:
            continue

        lowered = model_id.lower()
        if any(term in lowered for term in blocked_terms):
            continue

        seen_ids.add(model_id)
        model_name = str(row.get("name") or model_id)
        recommended = model_id == "openrouter/auto" or model_id.endswith(":free")
        models.append(
            {
                "id": model_id,
                "name": model_name,
                "recommended": recommended,
            }
        )

    if "openrouter/auto" not in seen_ids:
        models.insert(0, AI_HELP_FALLBACK_MODELS[0])

    models.sort(
        key=lambda item: (
            0 if item["id"] == "openrouter/auto" else 1,
            0 if str(item["id"]).endswith(":free") else 1,
            str(item["name"]).lower(),
        )
    )

    if not models:
        models = AI_HELP_FALLBACK_MODELS

    return {
        "models": models[:80],
        "default": default_model,
    }

# Announcements
@app.get("/api/guilds/{guild_id}/announcements")
@app.get("/api/bot/guilds/{guild_id}/announcements")
async def get_announcements(guild_id: str):
    data = load_json("announcement_drafts", {})
    guild_drafts = data.get(guild_id, {})
    
    # Return structured data with draft names as a separate field for easier editing
    result = {}
    for draft_name, draft_content in guild_drafts.items():
        result[draft_name] = {
            "name": draft_name,  # Editable name
            "content": draft_content  # Draft content
        }
    
    return result

@app.put("/api/guilds/{guild_id}/announcements")
@app.put("/api/bot/guilds/{guild_id}/announcements")
async def update_announcements(guild_id: str, drafts: Dict[str, Any]):
    normalized_drafts: Dict[str, Dict[str, Any]] = {}

    for draft_name, raw_draft in drafts.items():
        # Backward compatibility: some clients send { name, content: { ...draftFields } }
        candidate = raw_draft
        if (
            isinstance(raw_draft, dict)
            and isinstance(raw_draft.get("content"), dict)
        ):
            candidate = raw_draft.get("content", {})

        if not isinstance(candidate, dict):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid draft payload for '{draft_name}'"
            )

        validated = AnnouncementDraft.model_validate({
            "title": str(candidate.get("title", "")),
            "description": str(candidate.get("description", "")),
            "content": candidate.get("content"),
            "thumbnail_url": candidate.get("thumbnail_url"),
            "image_url": candidate.get("image_url"),
            "images": candidate.get("images") or [],
            "footer": candidate.get("footer"),
            "channel_id": candidate.get("channel_id"),
            "ping_role_id": candidate.get("ping_role_id"),
            "buttons": candidate.get("buttons") or [],
        })

        normalized_drafts[draft_name] = validated.model_dump()

    data = load_json("announcement_drafts", {})
    prev_guild = data.get(guild_id, {})
    if not isinstance(prev_guild, dict):
        prev_guild = {}
    # Shallow-merge at guild level, but deep-merge each draft so nested fields survive ``model_dump()``.
    merged_guild: Dict[str, Any] = {**prev_guild}
    for dname, norm in normalized_drafts.items():
        prev_draft = merged_guild.get(dname) if isinstance(merged_guild.get(dname), dict) else {}
        row = {**prev_draft, **norm}
        for sk in ("channel_id", "ping_role_id"):
            if row.get(sk) == "" or (isinstance(row.get(sk), str) and not str(row.get(sk)).strip()):
                row[sk] = None
        merged_guild[dname] = row
    data[guild_id] = merged_guild
    save_json("announcement_drafts", data)
    return {"status": "success"}

@app.post("/api/guilds/{guild_id}/upload_assets")
@app.post("/api/bot/guilds/{guild_id}/upload_assets")
async def upload_assets(request: Request, guild_id: str, file: UploadFile = File(...)):
    """Upload an asset to the local JSON-backed asset store."""
    # Verify auth
    user_token = _bearer_token(request)
    manageable = await _discord_user_manageable_guild_ids(user_token)
    if guild_id not in manageable:
        raise HTTPException(status_code=403, detail="Not authorized")

    content_type = file.content_type or "application/octet-stream"
    suffix = Path(file.filename or "").suffix.lower().lstrip(".") or "png"
    if len(suffix) > 8:
        suffix = "png"
    safe_name = f"{uuid.uuid4().hex[:12]}.{suffix}"
    asset_dir = _LOCAL_ASSET_ROOT / guild_id
    asset_dir.mkdir(parents=True, exist_ok=True)
    asset_path = asset_dir / safe_name

    try:
        file_bytes = await file.read()
        asset_path.write_bytes(file_bytes)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded image: {exc}") from exc

    public_url = f"/api/bot/assets/{guild_id}/{safe_name}"
    return {"status": "success", "url": public_url, "filename": safe_name, "content_type": content_type}


@app.get("/api/assets/{guild_id}/{filename}")
@app.get("/api/bot/assets/{guild_id}/{filename}")
async def get_uploaded_asset(guild_id: str, filename: str):
    safe_name = Path(filename).name
    asset_path = _LOCAL_ASSET_ROOT / guild_id / safe_name
    if not asset_path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")

    media_type = mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
    return FileResponse(str(asset_path), media_type=media_type, filename=asset_path.name)

@app.post("/api/guilds/{guild_id}/announcements/{old_name:path}/rename")
@app.post("/api/bot/guilds/{guild_id}/announcements/{old_name:path}/rename")
async def rename_announcement_draft(guild_id: str, old_name: str, body: Dict[str, str] = Body(...)):
    """Rename an existing announcement draft"""
    new_name = body.get("new_name", "").strip()
    
    if not new_name:
        raise HTTPException(status_code=400, detail="new_name is required")
    
    data = load_json("announcement_drafts", {})
    
    if guild_id not in data:
        raise HTTPException(status_code=404, detail="Guild not found")
    
    guild_drafts = data[guild_id]
    
    if old_name not in guild_drafts:
        raise HTTPException(status_code=404, detail="Draft not found")
    
    if new_name in guild_drafts and new_name != old_name:
        raise HTTPException(status_code=409, detail="A draft with this name already exists")
    
    # Rename the draft by copying to new key and deleting old; keep ``name`` column in sync for relational tables.
    if new_name != old_name:
        row = guild_drafts[old_name]
        if isinstance(row, dict):
            row["name"] = new_name
        guild_drafts[new_name] = row
        del guild_drafts[old_name]
        
    save_json("announcement_drafts", data)
    return {"status": "success", "old_name": old_name, "new_name": new_name}

@app.delete("/api/guilds/{guild_id}/announcements/{draft_name:path}")
@app.delete("/api/bot/guilds/{guild_id}/announcements/{draft_name:path}")
async def delete_announcement_draft(guild_id: str, draft_name: str):
    """Delete an existing announcement draft"""
    data = load_json("announcement_drafts", {})
    
    if guild_id not in data:
        raise HTTPException(status_code=404, detail="Guild not found")
    
    guild_drafts = data[guild_id]
    
    if draft_name not in guild_drafts:
        raise HTTPException(status_code=404, detail="Draft not found")
    
    # Delete the draft locally
    del guild_drafts[draft_name]
    save_json("announcement_drafts", data)
    
    return {"status": "success", "deleted_draft": draft_name}

# Poll Drafts
@app.get("/api/guilds/{guild_id}/polls")
@app.get("/api/bot/guilds/{guild_id}/polls")
async def get_polls(guild_id: str):
    data = load_json("poll_drafts", {})
    return data.get(guild_id, {})

@app.put("/api/guilds/{guild_id}/polls")
@app.put("/api/bot/guilds/{guild_id}/polls")
async def update_polls(guild_id: str, drafts: Dict[str, Any]):
    data = load_json("poll_drafts", {})
    data[guild_id] = _merge_shallow_nested_guild(data.get(guild_id), drafts if isinstance(drafts, dict) else {})
    save_json("poll_drafts", data)
    return {"status": "success"}


@app.get("/api/guilds/{guild_id}/polls/status")
@app.get("/api/bot/guilds/{guild_id}/polls/status")
async def get_polls_status(guild_id: str):
    data = load_json("polls", {})
    guild_data = data.get(guild_id, {})
    if not isinstance(guild_data, dict):
        guild_data = {}

    items: List[Dict[str, Any]] = []
    for message_id, raw_entry in guild_data.items():
        if not isinstance(raw_entry, dict):
            continue
        items.append(_normalize_poll_entry(raw_entry, guild_id=guild_id, message_id=str(message_id)))

    items.sort(
        key=lambda entry: (
            parse_iso_datetime(entry.get("created_at"))
            or parse_iso_datetime(entry.get("end_at"))
            or datetime.fromtimestamp(0, tz=timezone.utc)
        ),
        reverse=True,
    )

    now_utc = datetime.now(timezone.utc)
    active_count = 0
    for entry in items:
        ended = bool(entry.get("ended"))
        end_at = parse_iso_datetime(entry.get("end_at"))
        is_active = (not ended) and (end_at is None or end_at > now_utc)
        is_pending_end = (not ended) and end_at is not None and end_at <= now_utc
        entry["is_active"] = is_active
        entry["is_pending_end"] = is_pending_end
        if is_active:
            active_count += 1

    return {
        "active_count": active_count,
        "total_count": len(items),
        "items": items,
    }

# Giveaway Drafts
@app.get("/api/guilds/{guild_id}/giveaway_drafts")
@app.get("/api/bot/guilds/{guild_id}/giveaway_drafts")
async def get_giveaway_drafts(guild_id: str):
    data = load_json("giveaway_drafts", {})
    drafts = data.get(guild_id, {})
    return drafts if isinstance(drafts, dict) else {}

@app.put("/api/guilds/{guild_id}/giveaway_drafts")
@app.put("/api/bot/guilds/{guild_id}/giveaway_drafts")
async def update_giveaway_drafts(guild_id: str, drafts: Dict[str, Any]):
    data = load_json("giveaway_drafts", {})
    patch = drafts if isinstance(drafts, dict) else {}
    data[guild_id] = _merge_shallow_nested_guild(data.get(guild_id), patch)
    save_json("giveaway_drafts", data)
    return {"status": "success"}

# Giveaways (active + history indicator)
@app.get("/api/guilds/{guild_id}/giveaways")
@app.get("/api/bot/guilds/{guild_id}/giveaways")
async def get_giveaways(guild_id: str):
    data = load_json("giveaways", {})
    guild_data = data.get(guild_id, {})
    if not isinstance(guild_data, dict):
        guild_data = {}

    items: List[Dict[str, Any]] = []
    for message_id, raw_entry in guild_data.items():
        if not isinstance(raw_entry, dict):
            continue
        items.append(_normalize_giveaway_entry(raw_entry, guild_id=guild_id, message_id=str(message_id)))

    items.sort(
        key=lambda entry: (
            parse_iso_datetime(entry.get("created_at"))
            or parse_iso_datetime(entry.get("end_at"))
            or datetime.fromtimestamp(0, tz=timezone.utc)
        ),
        reverse=True,
    )

    now_utc = datetime.now(timezone.utc)
    active_count = 0
    for entry in items:
        ended = bool(entry.get("ended"))
        end_at = parse_iso_datetime(entry.get("end_at"))
        is_active = (not ended) and (end_at is None or end_at > now_utc)
        is_pending_end = (not ended) and end_at is not None and end_at <= now_utc
        entry["is_active"] = is_active
        entry["is_pending_end"] = is_pending_end
        if is_active:
            active_count += 1

    return {
        "active_count": active_count,
        "total_count": len(items),
        "items": items,
    }

# Tickets
@app.get("/api/guilds/{guild_id}/tickets")
async def get_tickets(guild_id: str):
    data = load_json("open_tickets", {})
    return stringify_ids(data.get(guild_id, TicketsConfigGuild().model_dump()))

@app.put("/api/guilds/{guild_id}/tickets")
async def update_tickets(guild_id: str, config: TicketsConfigGuild):
    data = load_json("open_tickets", {})
    prev = data.get(guild_id, {}) if isinstance(data.get(guild_id, {}), dict) else {}
    if guild_id in data and isinstance(data[guild_id], dict):
        config.open_tickets = data[guild_id].get("open_tickets", {})
    data[guild_id] = _merge_shallow_dict(prev, config.model_dump())
    save_json("open_tickets", data)
    return {"status": "success"}

# Key Panels
@app.get("/api/guilds/{guild_id}/keypanels")
async def get_key_panels(guild_id: str):
    data = load_json("key_panels", {})
    guild_panels = data.get(guild_id, {})
    return stringify_ids(guild_panels)

@app.put("/api/guilds/{guild_id}/keypanels")
async def update_key_panels(guild_id: str, config: Dict[str, KeyPanelConfig]):
    data = load_json("key_panels", {})
    data[guild_id] = {k: v.model_dump() for k, v in config.items()}
    save_json("key_panels", data)
    return {"status": "success"}

@app.delete("/api/guilds/{guild_id}/keypanels/{message_id}")
async def delete_key_panel(guild_id: str, message_id: str):
    data = load_json("key_panels", {})
    guild_panels = data.get(guild_id, {})
    if message_id in guild_panels:
        cfg = guild_panels[message_id]
        channel_id = cfg.get("channel_id")
        
        # Trigger Discord message deletion via IPC
        from modules.ipc import process_dashboard_trigger
        await process_dashboard_trigger("keypanel_delete", guild_id, {"message_id": message_id, "channel_id": channel_id})
        
        del guild_panels[message_id]
        save_json("key_panels", data)
        return {"status": "success"}
    else:
        raise HTTPException(status_code=404, detail="Key panel not found")

# Roblox Monitors
@app.get("/api/guilds/{guild_id}/roblox")
async def get_roblox(guild_id: str):
    data = load_json("roblox_monitors", {})
    # Stored data may use an int guild_id; match by string comparison.
    guild_rows = data.get(guild_id) or data.get(int(guild_id) if guild_id.isdigit() else guild_id, [])
    if not isinstance(guild_rows, list):
        guild_rows = [guild_rows] if guild_rows else []
    # Remap any stored columns to expected field names.
    normalized = []
    for row in guild_rows:
        if not isinstance(row, dict):
            continue
        normalized.append({
            "name": row.get("name"),
            "universe_id": str(row["universe_id"]) if row.get("universe_id") else None,
            "channel_id": str(row["channel_id"]) if row.get("channel_id") else None,
            "role_id": str(row["role_id"]) if row.get("role_id") else None,
            "last_updated": row.get("last_updated"),
        })
    return normalized

@app.put("/api/guilds/{guild_id}/roblox")
async def update_roblox(guild_id: str, monitors: List[RobloxMonitor]):
    data = load_json("roblox_monitors", {})
    data[guild_id] = [m.model_dump() for m in monitors]
    save_json("roblox_monitors", data)
    return {"status": "success"}

@app.get("/api/guilds/{guild_id}/roblox/health")
@app.get("/api/bot/guilds/{guild_id}/roblox/health")
async def get_roblox_health(guild_id: str):
    monitors_data = load_json("roblox_monitors", {})
    guild_monitors = monitors_data.get(guild_id, [])
    if isinstance(guild_monitors, dict):
        guild_monitors = [guild_monitors]
    if not isinstance(guild_monitors, list):
        guild_monitors = []

    health = load_json("roblox_monitor_health", {})
    last_poll_started = health.get("last_poll_started")
    last_poll_finished = health.get("last_poll_finished")

    last_poll_dt = parse_iso_datetime(last_poll_finished)
    age_seconds = None
    if last_poll_dt is not None:
        age_seconds = max(0, int((datetime.now(timezone.utc) - last_poll_dt).total_seconds()))

    # Poll interval is 5m; consider stale after 11m to allow jitter/restarts.
    stale_after_seconds = 11 * 60
    is_stale = age_seconds is None or age_seconds > stale_after_seconds
    has_error = bool(health.get("last_error"))
    loop_healthy = not is_stale and not has_error

    return {
        "monitor_count": len(guild_monitors),
        "loop_healthy": loop_healthy,
        "is_stale": is_stale,
        "age_seconds": age_seconds,
        "loop_started_at": health.get("loop_started_at"),
        "last_poll_started": last_poll_started,
        "last_poll_finished": last_poll_finished,
        "last_poll_seconds": health.get("last_poll_seconds"),
        "last_notifications": health.get("last_notifications", 0),
        "last_error": health.get("last_error"),
    }

# Social Monitors
@app.get("/api/guilds/{guild_id}/social")
@app.get("/api/bot/guilds/{guild_id}/social")
async def get_social(guild_id: str):
    data = load_json("social_monitors", {})
    # Stored data may use an int guild_id; match by string comparison.
    guild_rows = data.get(guild_id) or data.get(int(guild_id) if guild_id.isdigit() else guild_id, [])
    if not isinstance(guild_rows, list):
        guild_rows = [guild_rows] if guild_rows else []
    # Remap stored column names to the field names the bot/frontend expects.
    normalized = []
    for row in guild_rows:
        if not isinstance(row, dict):
            continue
        normalized.append({
            "name": row.get("name"),
            "platform": row.get("platform", "rss"),
            # Stored data may use 'username' instead of 'source'.
            "source": row.get("source") or row.get("username") or "",
            "channel_id": str(row["channel_id"]) if row.get("channel_id") else None,
            "role_id": str(row["role_id"]) if row.get("role_id") else None,
            "enabled": bool(row.get("enabled", True)),
            # Stored data may use 'last_post_id' instead of 'last_entry_id'.
            "last_entry_id": row.get("last_entry_id") or row.get("last_post_id"),
            "last_checked_at": row.get("last_checked_at"),
            "last_posted_at": row.get("last_posted_at"),
            "last_error": row.get("last_error"),
        })
    return stringify_ids(normalized)

@app.put("/api/guilds/{guild_id}/social")
@app.put("/api/bot/guilds/{guild_id}/social")
async def update_social(guild_id: str, monitors: List[SocialMonitor]):
    data = load_json("social_monitors", {})

    # Build a lookup of existing rows keyed by source so we can preserve
    # runtime-tracked fields (last_entry_id, last_checked_at, last_posted_at,
    # last_error) that the frontend never sends. Without this merge, saving
    # settings would wipe last_entry_id → bot re-posts the latest video on
    # every restart because it looks like a fresh first-run seed.
    existing_rows = data.get(guild_id, [])
    if not isinstance(existing_rows, list):
        existing_rows = [existing_rows] if existing_rows else []
    existing_by_source: dict = {}
    for row in existing_rows:
        if isinstance(row, dict) and row.get("source"):
            existing_by_source[str(row["source"]).strip()] = row

    _RUNTIME_FIELDS = ("last_entry_id", "last_checked_at", "last_posted_at", "last_error")

    merged = []
    for m in monitors:
        row = m.model_dump()
        existing = existing_by_source.get(str(m.source or "").strip(), {})
        for field in _RUNTIME_FIELDS:
            # Only keep existing value if the incoming value is None/empty
            if not row.get(field) and existing.get(field):
                row[field] = existing[field]
        merged.append(row)

    data[guild_id] = merged
    save_json("social_monitors", data)
    return {"status": "success"}


@app.post("/api/guilds/{guild_id}/social/test")
@app.post("/api/bot/guilds/{guild_id}/social/test")
async def test_social(guild_id: str, monitor: SocialMonitor):
    platform = str(monitor.platform or "rss").strip().lower()
    source = str(monitor.source or "").strip()

    if not source:
        raise HTTPException(status_code=422, detail="Provide Source before testing.")

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25),
            headers={"User-Agent": "SeisenHubBot/1.0 (+social-monitor-test)"},
        ) as session:
            if platform == "youtube":
                resolved_source_url, latest_entry = await _social_fetch_latest_youtube_entry(source, session)
            else:
                resolved_source_url = _social_resolve_source_url(platform, source)
                if not resolved_source_url:
                    raise HTTPException(
                        status_code=422,
                        detail="Could not resolve source URL. Use a valid URL, or for TikTok use @handle.",
                    )
                latest_entry = await _social_fetch_latest_source_entry(resolved_source_url, session)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=_social_test_error_detail(e))

    if not latest_entry:
        raise HTTPException(status_code=422, detail="Source resolved but no entries were found yet.")

    published_at = latest_entry.get("published_at")
    return {
        "status": "success",
        "guild_id": guild_id,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "resolved_source_url": resolved_source_url,
        "latest_entry": {
            "id": latest_entry.get("id"),
            "title": latest_entry.get("title"),
            "author": latest_entry.get("author"),
            "link": latest_entry.get("link"),
            "thumbnail": latest_entry.get("thumbnail"),
            "published_at": published_at.isoformat() if isinstance(published_at, datetime) else None,
        },
    }

@app.get("/api/guilds/{guild_id}/social/health")
@app.get("/api/bot/guilds/{guild_id}/social/health")
async def get_social_health(guild_id: str):
    monitors_data = load_json("social_monitors", {})
    guild_monitors = monitors_data.get(guild_id, [])
    if isinstance(guild_monitors, dict):
        guild_monitors = [guild_monitors]
    if not isinstance(guild_monitors, list):
        guild_monitors = []

    enabled_count = sum(
        1
        for monitor in guild_monitors
        if isinstance(monitor, dict) and monitor.get("enabled", True)
    )

    health = load_json("social_monitor_health", {})
    last_poll_started = health.get("last_poll_started")
    last_poll_finished = health.get("last_poll_finished")

    last_poll_dt = parse_iso_datetime(last_poll_finished)
    age_seconds = None
    if last_poll_dt is not None:
        age_seconds = max(0, int((datetime.now(timezone.utc) - last_poll_dt).total_seconds()))

    # Poll interval is 3m; consider stale after 8m to allow jitter/restarts.
    stale_after_seconds = 8 * 60
    is_stale = age_seconds is None or age_seconds > stale_after_seconds
    has_error = bool(health.get("last_error"))
    loop_healthy = not is_stale and not has_error

    return {
        "guild_monitor_count": len(guild_monitors),
        "enabled_monitor_count": enabled_count,
        "loop_healthy": loop_healthy,
        "is_stale": is_stale,
        "age_seconds": age_seconds,
        "loop_started_at": health.get("loop_started_at"),
        "last_poll_started": last_poll_started,
        "last_poll_finished": last_poll_finished,
        "last_poll_seconds": health.get("last_poll_seconds"),
        "last_notifications": health.get("last_notifications", 0),
        "last_error": health.get("last_error"),
    }

# Fun Commands (per-guild overlay in fun_config.json → guilds[guildId])
@app.get("/api/guilds/{guild_id}/fun-commands")
async def get_fun_commands(guild_id: str):
    """Get fun command responses configuration for this guild."""
    return _fun_get_guild_payload(guild_id)


@app.put("/api/guilds/{guild_id}/fun-commands")
async def update_fun_commands(guild_id: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    """Update fun command responses for this guild only."""
    _fun_put_guild_payload(guild_id, payload)
    return {"status": "success"}

# Onboarding (Welcome + Join Guard)
@app.get("/api/guilds/{guild_id}/onboarding")
@app.get("/api/bot/guilds/{guild_id}/onboarding")
async def get_onboarding(guild_id: str):
    data = load_json("onboarding_configs", {})
    default_config = OnboardingConfigGuild().model_dump()
    merged = {**default_config, **(data.get(guild_id, {}) or {})}
    return stringify_ids(merged)

@app.put("/api/guilds/{guild_id}/onboarding")
@app.put("/api/bot/guilds/{guild_id}/onboarding")
async def update_onboarding(guild_id: str, request: Request, payload: Dict[str, Any] = Body(default_factory=dict)):
    data = load_json("onboarding_configs", {})
    existing = data.get(guild_id, {}) if isinstance(data.get(guild_id, {}), dict) else {}

    combined_payload = _merge_shallow_dict(existing, payload if isinstance(payload, dict) else {})
    next_cfg = _coerce_onboarding_payload(combined_payload)

    data[guild_id] = next_cfg
    save_json("onboarding_configs", data)
    await _append_config_audit_entry(
        request=request,
        guild_id=guild_id,
        module="onboarding",
        before=existing,
        after=next_cfg,
    )
    return {"status": "success"}

@app.get("/api/roblox/{universe_id}")
async def get_roblox_info(request: Request, universe_id: str):
    """Proxy to fetch Roblox game info and thumbnail to bypass CORS for the dashboard."""
    await require_authenticated_discord_user(request)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20),
        connector=aiohttp.TCPConnector(ssl=_SOCIAL_SSL_CTX),
    ) as session:
        # Fetch Game Info
        game_url = f"https://games.roblox.com/v1/games?universeIds={universe_id}"
        async with session.get(game_url) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=404, detail="Game not found")
            game_data = await resp.json()
            games = game_data.get("data", [])
            gameInfo = games[0] if games else None

        if not gameInfo:
            raise HTTPException(status_code=404, detail="Game not found")

        # Fetch Thumbnail
        thumb_url = f"https://thumbnails.roblox.com/v1/games/icons?universeIds={universe_id}&size=512x512&format=Png&isCircular=false"
        async with session.get(thumb_url) as resp:
            thumb_data = await resp.json()
            items = thumb_data.get("data", [])
            thumbnail = items[0].get("imageUrl") if items else None
            
        return {
            "name": gameInfo.get("name"),
            "playing": gameInfo.get("playing", 0),
            "visits": gameInfo.get("visits", 0),
            "thumbnail_url": thumbnail
        }

# Sticky
@app.get("/api/guilds/{guild_id}/sticky")
async def get_sticky(guild_id: str):
    data = load_json("stickies", {})
    raw = data.get(guild_id, {})
    if not isinstance(raw, dict):
        return {}
    # Store one row per guild with channel_id as a field.
    # The frontend expects {channel_id: {name, title, content, color}}
    if "channel_id" in raw and "content" in raw:
        # Single flat row — convert to channel-keyed dict
        channel_id = str(raw.get("channel_id") or "").strip()
        if channel_id:
            return stringify_ids({channel_id: {
                "name": raw.get("name"),
                "title": raw.get("title"),
                "content": raw.get("content", ""),
                "color": raw.get("color"),
            }})
    # Already in {channel_id: {...}} dict shape
    return stringify_ids(raw)

@app.put("/api/guilds/{guild_id}/sticky")
async def update_sticky(guild_id: str, config: Dict[str, StickyConfig]):
    data = load_json("stickies", {})
    existing = data.get(guild_id, {})
    if not isinstance(existing, dict):
        existing = {}
    merged: Dict[str, Any] = {**existing}
    for ch_id, sc in config.items():
        prev = merged.get(ch_id)
        if not isinstance(prev, dict):
            prev = {}
        merged[ch_id] = {**prev, **sc.model_dump()}
    data[guild_id] = merged
    save_json("stickies", data)
    return {"status": "success"}

# Boost
@app.get("/api/guilds/{guild_id}/boost")
async def get_boost(guild_id: str):
    data = load_json("boost_configs", {})
    raw = data.get(guild_id, {})
    if not isinstance(raw, dict):
        raw = {}
    # Return only the fields the frontend needs (strip guild_id)
    return stringify_ids({
        "category_id": raw.get("category_id"),
        "channel_id": raw.get("channel_id"),
        "roles": raw.get("roles", []),
    })

@app.put("/api/guilds/{guild_id}/boost")
async def update_boost(guild_id: str, config: BoostConfigGuild):
    data = load_json("boost_configs", {})
    prev = data.get(guild_id, {}) if isinstance(data.get(guild_id, {}), dict) else {}
    data[guild_id] = _merge_shallow_dict(prev, config.model_dump())
    save_json("boost_configs", data)
    return {"status": "success"}

# Member Counter (dashboard uses member-counter; bot module uses member_counter_config.json)
@app.get("/api/guilds/{guild_id}/member_counter")
@app.get("/api/bot/guilds/{guild_id}/member_counter")
@app.get("/api/guilds/{guild_id}/member-counter")
@app.get("/api/bot/guilds/{guild_id}/member-counter")
async def get_member_counter(guild_id: str):
    data = load_json("member_counter_configs", {})
    raw = data.get(guild_id, {})
    if not isinstance(raw, dict):
        raw = {}

    # Coerce legacy numeric snowflake fields to strings before model validation.
    for key in ("channel_id", "category_id", "created_by"):
        if key in raw:
            raw[key] = _coerce_optional_str(raw.get(key))

    merged = MemberCounterConfigGuild(**{**MemberCounterConfigGuild().model_dump(), **raw})
    payload = merged.model_dump()
    payload["channel_type"] = "text" if str(payload.get("channel_type")).lower() == "text" else "voice"
    payload["prefix"] = str(payload.get("prefix") or "Members: ")
    return stringify_ids(payload)

@app.put("/api/guilds/{guild_id}/member_counter")
@app.put("/api/bot/guilds/{guild_id}/member_counter")
@app.put("/api/guilds/{guild_id}/member-counter")
@app.put("/api/bot/guilds/{guild_id}/member-counter")
async def update_member_counter(guild_id: str, config: MemberCounterConfigGuild, request: Request):
    data = load_json("member_counter_configs", {})
    before_cfg = data.get(guild_id, {}) if isinstance(data.get(guild_id, {}), dict) else {}
    payload = config.model_dump()
    payload["channel_type"] = "text" if str(payload.get("channel_type")).lower() == "text" else "voice"
    payload["prefix"] = str(payload.get("prefix") or "Members: ")[:90]
    data[guild_id] = _merge_shallow_dict(before_cfg, payload)
    save_json("member_counter_configs", data)
    await _append_config_audit_entry(
        request=request,
        guild_id=guild_id,
        module="member_counter",
        before=before_cfg,
        after=payload,
    )
    return {"status": "success"}

# ── Role Counter Panels ────────────────────────────────────────────────────────

_ROLE_PANEL_FILE = "role_panel_configs"


def _normalize_role_panel(raw: dict) -> dict:
    return {
        "id": str(raw.get("id") or uuid.uuid4()),
        "channel_id": str(raw.get("channel_id") or ""),
        "message_id": str(raw.get("message_id") or "") or None,
        "title": str(raw.get("title") or "Role Members"),
        "role_ids": [str(r) for r in (raw.get("role_ids") or []) if r],
        "enabled": bool(raw.get("enabled", True)),
        "last_updated": str(raw.get("last_updated") or "") or None,
    }


@app.get("/api/guilds/{guild_id}/role_counters")
@app.get("/api/bot/guilds/{guild_id}/role_counters")
async def get_role_counters(guild_id: str):
    root = load_json(_ROLE_PANEL_FILE, {})
    raw_list = root.get(guild_id, [])
    if not isinstance(raw_list, list):
        raw_list = []
    return stringify_ids({"panels": [_normalize_role_panel(e) for e in raw_list if isinstance(e, dict)]})


@app.post("/api/guilds/{guild_id}/role_counters")
@app.post("/api/bot/guilds/{guild_id}/role_counters")
async def create_role_counter(guild_id: str, request: Request, body: Dict[str, Any] = Body(...)):
    root = load_json(_ROLE_PANEL_FILE, {})
    panels = root.get(guild_id, [])
    if not isinstance(panels, list):
        panels = []
    entry = _normalize_role_panel({**body, "id": str(uuid.uuid4())})
    panels.append(entry)
    root[guild_id] = panels
    save_json(_ROLE_PANEL_FILE, root)
    await _append_config_audit_entry(request=request, guild_id=guild_id, module="role_counters", before=None, after=entry)
    return stringify_ids({"status": "success", "panel": entry})


@app.put("/api/guilds/{guild_id}/role_counters/{panel_id}")
@app.put("/api/bot/guilds/{guild_id}/role_counters/{panel_id}")
async def update_role_counter(guild_id: str, panel_id: str, request: Request, body: Dict[str, Any] = Body(...)):
    root = load_json(_ROLE_PANEL_FILE, {})
    panels = root.get(guild_id, [])
    if not isinstance(panels, list):
        panels = []
    before = None
    updated = None
    new_list = []
    for e in panels:
        if not isinstance(e, dict):
            continue
        if str(e.get("id")) == panel_id:
            before = dict(e)
            updated = _normalize_role_panel({**e, **body, "id": panel_id})
            new_list.append(updated)
        else:
            new_list.append(e)
    if updated is None:
        raise HTTPException(status_code=404, detail="Panel not found.")
    root[guild_id] = new_list
    save_json(_ROLE_PANEL_FILE, root)
    await _append_config_audit_entry(request=request, guild_id=guild_id, module="role_counters", before=before, after=updated)
    return stringify_ids({"status": "success", "panel": updated})


@app.delete("/api/guilds/{guild_id}/role_counters/{panel_id}")
@app.delete("/api/bot/guilds/{guild_id}/role_counters/{panel_id}")
async def delete_role_counter(guild_id: str, panel_id: str, request: Request):
    root = load_json(_ROLE_PANEL_FILE, {})
    panels = root.get(guild_id, [])
    if not isinstance(panels, list):
        panels = []
    root[guild_id] = [e for e in panels if isinstance(e, dict) and str(e.get("id")) != panel_id]
    save_json(_ROLE_PANEL_FILE, root)
    return {"status": "success"}


@app.post("/api/guilds/{guild_id}/role_counters/{panel_id}/post")
@app.post("/api/bot/guilds/{guild_id}/role_counters/{panel_id}/post")
async def post_role_counter_panel(guild_id: str, panel_id: str, request: Request):
    from modules.role_counter import post_panel
    await require_guild_dashboard_access(request, guild_id)
    ok, detail = await post_panel(int(guild_id), panel_id)
    if not ok:
        raise HTTPException(status_code=400, detail=detail)
    return {"status": "success", "message_id": detail}


@app.post("/api/guilds/{guild_id}/role_counters/{panel_id}/sync")
@app.post("/api/bot/guilds/{guild_id}/role_counters/{panel_id}/sync")
async def sync_role_counter_panel(guild_id: str, panel_id: str, request: Request):
    from modules.role_counter import sync_panel
    await require_guild_dashboard_access(request, guild_id)
    ok, message = await sync_panel(int(guild_id), panel_id)
    return {"status": "success" if ok else "noop", "message": message}


# Vouch
@app.get("/api/guilds/{guild_id}/vouch")
async def get_vouch(guild_id: str):
    data = load_json("vouch_configs", {})
    raw = data.get(guild_id)
    # raw might be: int, str, None, or a full row dict {guild_id, channel_id}
    if isinstance(raw, dict):
        channel_id = raw.get("channel_id")
        return stringify_ids({"channel_id": str(channel_id) if channel_id else None})
    return {"channel_id": str(raw) if raw and isinstance(raw, int) else (raw or None)}

@app.put("/api/guilds/{guild_id}/vouch")
async def update_vouch(guild_id: str, config: Dict[str, Any]):
    data = load_json("vouch_configs", {})
    data[guild_id] = config.get("channel_id")
    save_json("vouch_configs", data)
    return {"status": "success"}

class CommandAccessConfig(BaseModel):
    commands: Dict[str, List[str]]

# Command Access
@app.get("/api/commands/available")
async def get_available_commands():
    """Return the full list of owner-lockable commands the bot knows about."""
    from modules.access_control import OWNER_LOCKED_COMMANDS
    return {"commands": sorted(OWNER_LOCKED_COMMANDS)}

@app.get("/api/guilds/{guild_id}/commands")
async def get_commands(guild_id: str):
    data = load_json("command_access", {})
    stored = data.get(guild_id, [])
    commands_map: Dict[str, List[str]] = {}
    # command_access is stored as a list of rows per guild (one row per command)
    if isinstance(stored, list):
        for row in stored:
            if not isinstance(row, dict):
                continue
            cmd = str(row.get("command_name") or "").strip()
            role_ids = row.get("role_ids") or []
            if cmd:
                commands_map[cmd] = [str(r) for r in role_ids]
    elif isinstance(stored, dict):
        # Legacy dict shape like {commands: {cmd: [roleIds]}}
        if "commands" in stored:
            return stringify_ids(stored)
        # Or single row shape with command_name key
        if "command_name" in stored:
            cmd = str(stored.get("command_name") or "").strip()
            role_ids = stored.get("role_ids") or []
            if cmd:
                commands_map[cmd] = [str(r) for r in role_ids]
    return stringify_ids({"commands": commands_map})

@app.put("/api/guilds/{guild_id}/commands")
async def update_commands(guild_id: str, config: CommandAccessConfig):
    data = load_json("command_access", {})
    prev = data.get(guild_id)
    dump = config.model_dump()
    if isinstance(prev, dict):
        merged = _merge_shallow_dict(prev, dump)
        p_cmds = prev.get("commands")
        d_cmds = dump.get("commands")
        if isinstance(p_cmds, dict) and isinstance(d_cmds, dict):
            merged["commands"] = {**p_cmds, **d_cmds}
        data[guild_id] = merged
    else:
        data[guild_id] = dump
    save_json("command_access", data)
    return {"status": "success"}

# Application Panel Config
@app.get("/api/guilds/{guild_id}/apppanel")
async def get_apppanel(guild_id: str):
    data = load_json("app_panels", {})
    cfg = data.get(guild_id, {})
    return stringify_ids(cfg)

@app.put("/api/guilds/{guild_id}/apppanel")
async def save_apppanel(guild_id: str, body: Dict[str, Any] = Body(...)):
    data = load_json("app_panels", {})
    existing = data.get(guild_id, {})
    data[guild_id] = {**existing, **body}
    save_json("app_panels", data)
    return stringify_ids({"status": "success", "config": data[guild_id]})

# Applications
@app.get("/api/guilds/{guild_id}/applications")
async def get_applications(guild_id: str, status: Optional[str] = None, position: Optional[str] = None):
    data = load_json("applications", {})
    apps = data.get(guild_id, [])
    if not isinstance(apps, list):
        apps = []
    if status:
        apps = [a for a in apps if a.get("status") == status]
    if position:
        apps = [a for a in apps if a.get("position") == position]
    apps_sorted = sorted(apps, key=lambda a: a.get("submitted_at", ""), reverse=True)
    counts = {"pending": 0, "accepted": 0, "rejected": 0}
    for a in data.get(guild_id, []) if isinstance(data.get(guild_id, []), list) else []:
        s = a.get("status", "pending")
        if s in counts:
            counts[s] += 1
    return stringify_ids({"applications": apps_sorted, "total": len(apps_sorted), "counts": counts})


@app.put("/api/guilds/{guild_id}/applications/{app_id}")
async def update_application(guild_id: str, app_id: str, body: Dict[str, Any] = Body(...)):
    data = load_json("applications", {})
    apps = data.get(guild_id, [])
    if not isinstance(apps, list):
        raise HTTPException(status_code=404, detail="No applications found")
    for i, app in enumerate(apps):
        if app.get("id") == app_id:
            for field in ("status", "notes", "reviewed_by", "reviewed_at"):
                if field in body:
                    apps[i][field] = body[field]
            if "status" in body and not apps[i].get("reviewed_at"):
                apps[i]["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            data[guild_id] = apps
            save_json("applications", data)

            # Auto-assign role when accepted
            if body.get("status") == "accepted":
                try:
                    panel_cfg = load_json("app_panels", {}).get(guild_id, {})
                    accept_roles = panel_cfg.get("accept_roles", {})
                    position = apps[i].get("position", "")
                    role_id = accept_roles.get(position)
                    user_id = apps[i].get("user_id")
                    if role_id and user_id:
                        from modules.ipc import process_dashboard_trigger
                        await process_dashboard_trigger("apppanel_assign_role", guild_id, {
                            "user_id": user_id,
                            "role_id": role_id,
                        })
                except Exception:
                    pass  # Role assignment failure should not block the accept response

            return stringify_ids({"status": "success", "application": apps[i]})
    raise HTTPException(status_code=404, detail="Application not found")


# Reaction Roles
@app.get("/api/guilds/{guild_id}/reaction_roles")
async def get_reaction_roles(guild_id: str):
    data = load_json("reaction_roles", {})
    guild_messages = {}
    
    for message_id, message_data in data.items():
        if str(message_data.get("guild_id")) == guild_id:
            guild_messages[message_id] = message_data
    
    return guild_messages

@app.post("/api/guilds/{guild_id}/reaction_roles")
async def create_reaction_role(guild_id: str, config: Dict[str, Any]):
    from modules.ipc import process_dashboard_trigger
    result, status_code = await process_dashboard_trigger("create_reaction_role", guild_id, config)
    if status_code != 200:
        raise HTTPException(status_code=status_code, detail=result.get("message") or str(result))
    return result

@app.put("/api/guilds/{guild_id}/reaction_roles/{message_id}")
async def update_reaction_role(guild_id: str, message_id: str, config: Dict[str, Any]):
    from modules.ipc import process_dashboard_trigger
    payload = {"message_id": message_id, **config}
    result, status_code = await process_dashboard_trigger("update_reaction_role", guild_id, payload)
    if status_code != 200:
        raise HTTPException(status_code=status_code, detail=result.get("message") or str(result))
    return result

@app.delete("/api/guilds/{guild_id}/reaction_roles/{message_id}")
async def delete_reaction_role(guild_id: str, message_id: str):
    data = load_json("reaction_roles", {})
    
    if message_id in data and str(data[message_id].get("guild_id")) == guild_id:
        del data[message_id]
        save_json("reaction_roles", data)
        return {"status": "success"}
    else:
        raise HTTPException(status_code=404, detail="Reaction role message not found")

# Select Menu Roles (Drafts)
@app.get("/api/guilds/{guild_id}/select_menu_roles")
@app.get("/api/bot/guilds/{guild_id}/select_menu_roles")
async def get_select_menu_roles(guild_id: str):
    data = load_json("select_menu_drafts", {})
    return data.get(guild_id, {})

@app.put("/api/guilds/{guild_id}/select_menu_roles")
@app.put("/api/bot/guilds/{guild_id}/select_menu_roles")
async def update_select_menu_roles(guild_id: str, drafts: Dict[str, Any]):
    data = load_json("select_menu_drafts", {})
    patch = drafts if isinstance(drafts, dict) else {}
    data[guild_id] = _merge_shallow_nested_guild(data.get(guild_id), patch)
    save_json("select_menu_drafts", data)
    return {"status": "success"}

@app.post("/api/guilds/{guild_id}/select_menu_roles/{old_name:path}/rename")
@app.post("/api/bot/guilds/{guild_id}/select_menu_roles/{old_name:path}/rename")
async def rename_select_menu_draft(guild_id: str, old_name: str, body: Dict[str, str] = Body(...)):
    """Rename an existing select menu draft"""
    new_name = body.get("new_name", "").strip()
    
    if not new_name:
        raise HTTPException(status_code=400, detail="new_name is required")
    
    data = load_json("select_menu_drafts", {})
    if guild_id not in data:
        raise HTTPException(status_code=404, detail="Guild not found")
    
    guild_drafts = data[guild_id]
    if old_name not in guild_drafts:
        raise HTTPException(status_code=404, detail="Draft not found")
    if new_name in guild_drafts and new_name != old_name:
        raise HTTPException(status_code=409, detail="A draft with this name already exists")
    
    if new_name != old_name:
        row = guild_drafts[old_name]
        if isinstance(row, dict):
            row["name"] = new_name
        guild_drafts[new_name] = row
        del guild_drafts[old_name]

    save_json("select_menu_drafts", data)
    return {"status": "success"}

@app.delete("/api/guilds/{guild_id}/select_menu_roles/{draft_name:path}")
@app.delete("/api/bot/guilds/{guild_id}/select_menu_roles/{draft_name:path}")
async def delete_select_menu_draft(guild_id: str, draft_name: str):
    """Delete an existing select menu draft"""
    data = load_json("select_menu_drafts", {})
    
    if guild_id not in data:
        raise HTTPException(status_code=404, detail="Guild not found")
    guild_drafts = data[guild_id]
    if draft_name not in guild_drafts:
        raise HTTPException(status_code=404, detail="Draft not found")
    
    del guild_drafts[draft_name]
    save_json("select_menu_drafts", data)
    
    return {"status": "success"}

@app.get("/api/guilds/{guild_id}/select_menu_roles/import")
@app.get("/api/bot/guilds/{guild_id}/select_menu_roles/import")
async def import_select_menu_role(request: Request, guild_id: str, message_link: str = ""):
    """
    Import an existing Discord select menu role message into the dashboard.
    Accepts a full Discord message link:
      https://discord.com/channels/{guild}/{channel}/{message}
    """
    await require_guild_dashboard_access(request, guild_id)

    raw = (message_link or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="message_link is required")

    match = _DISCORD_MSG_LINK_RE.search(raw)
    if not match:
        raise HTTPException(
            status_code=400,
            detail="Invalid message link. Paste a full Discord message link: https://discord.com/channels/GUILD/CHANNEL/MESSAGE"
        )

    link_guild_id = match.group(1)
    channel_id = match.group(2)
    message_id = match.group(3)

    if link_guild_id != str(guild_id):
        raise HTTPException(status_code=400, detail="Message link belongs to a different server.")

    # Check local select_menu_roles storage first (fast path)
    from modules.select_menu_roles import load_select_menu_roles
    stored = load_select_menu_roles()
    if message_id in stored:
        entry = stored[message_id]
        msg_data = entry.get("message_data", {})
        embeds = msg_data.get("embeds", [{}])
        embed_data = embeds[0] if embeds else {}
        components = msg_data.get("components", [])
        options = []
        placeholder = "Choose roles..."
        min_values = 1
        max_values = 1
        for row in components:
            if row.get("type") == 1:
                for comp in row.get("components", []):
                    if comp.get("type") == 3:
                        placeholder = comp.get("placeholder", placeholder)
                        min_values = comp.get("min_values", min_values)
                        max_values = comp.get("max_values", max_values)
                        for opt in comp.get("options", []):
                            options.append({
                                "label": opt.get("label", ""),
                                "value": opt.get("value", ""),
                                "description": opt.get("description", ""),
                                "emoji": opt.get("emoji"),
                            })

        raw_color = embed_data.get("color", "#5865F2")
        color_hex = f"#{raw_color:06X}" if isinstance(raw_color, int) else str(raw_color)

        raw_desc = embed_data.get("description", "")
        if isinstance(raw_desc, list):
            raw_desc = "\n".join(str(x) for x in raw_desc)

        thumbnail_url = ""
        thumb = embed_data.get("thumbnail")
        if isinstance(thumb, dict):
            thumbnail_url = thumb.get("url", "")
        elif isinstance(thumb, str):
            thumbnail_url = thumb

        footer_text = ""
        footer = embed_data.get("footer")
        if isinstance(footer, dict):
            footer_text = footer.get("text", "")
        elif isinstance(footer, str):
            footer_text = footer

        return {
            "message_id": message_id,
            "channel_id": channel_id,
            "title": embed_data.get("title", ""),
            "description": raw_desc,
            "color": color_hex,
            "thumbnail_url": thumbnail_url,
            "footer": footer_text,
            "content": msg_data.get("content") or "",
            "placeholder": placeholder,
            "min_values": min_values,
            "max_values": max_values,
            "options": options,
            "source": "stored",
        }

    # Not in local storage — fetch from Discord using bot token
    if not DISCORD_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Bot token not configured")

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=_DASHBOARD_UA,
        connector=aiohttp.TCPConnector(ssl=_SOCIAL_SSL_CTX),
    ) as session:
        status, body = await _discord_bot_get_json_with_retry(
            session,
            f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        )

    if status == 404:
        raise HTTPException(status_code=404, detail="Message not found. Make sure the bot has access to that channel.")
    if status == 403:
        raise HTTPException(status_code=403, detail="Bot does not have permission to read that channel.")
    if status != 200:
        raise HTTPException(status_code=status, detail=f"Discord API error: {body.get('message', body) if isinstance(body, dict) else body}")

    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Discord returned an unexpected response.")

    # Parse the fetched message
    embeds = body.get("embeds", [])
    embed_data = embeds[0] if embeds else {}
    components = body.get("components", [])

    options = []
    placeholder = "Choose roles..."
    min_values = 1
    max_values = 1

    for row in components:
        if row.get("type") == 1:
            for comp in row.get("components", []):
                if comp.get("type") == 3:
                    placeholder = comp.get("placeholder", placeholder)
                    min_values = comp.get("min_values", min_values)
                    max_values = comp.get("max_values", max_values)
                    for opt in comp.get("options", []):
                        emoji_data = opt.get("emoji")
                        emoji_str = None
                        if isinstance(emoji_data, dict):
                            emoji_str = emoji_data.get("name") or None
                        elif isinstance(emoji_data, str):
                            emoji_str = emoji_data
                        options.append({
                            "label": opt.get("label", ""),
                            "value": opt.get("value", ""),
                            "description": opt.get("description", ""),
                            "emoji": emoji_str,
                        })

    raw_color = embed_data.get("color", 0x5865F2)
    color_hex = f"#{raw_color:06X}" if isinstance(raw_color, int) else str(raw_color or "#5865F2")

    raw_desc = embed_data.get("description", "") or ""
    if isinstance(raw_desc, list):
        raw_desc = "\n".join(str(x) for x in raw_desc)

    thumbnail_url = ""
    thumb = embed_data.get("thumbnail")
    if isinstance(thumb, dict):
        thumbnail_url = thumb.get("url", "")

    footer_text = ""
    footer_data = embed_data.get("footer")
    if isinstance(footer_data, dict):
        footer_text = footer_data.get("text", "")

    return {
        "message_id": message_id,
        "channel_id": channel_id,
        "title": embed_data.get("title", ""),
        "description": raw_desc,
        "color": color_hex,
        "thumbnail_url": thumbnail_url,
        "footer": footer_text,
        "content": body.get("content") or "",
        "placeholder": placeholder,
        "min_values": min_values,
        "max_values": max_values,
        "options": options,
        "source": "discord",
    }


# --- Activity Rewards ---


_ACTIVITY_REWARDS_CONFIG_PATH = "activity_rewards_configs"
_ACTIVITY_REWARDS_STATS_PATH = "activity_rewards_stats"
_ACTIVITY_REWARDS_STATE_PATH = "activity_rewards_states"
_ACTIVITY_REWARDS_DEFAULT = {
    "enabled": _coerce_bool(os.getenv("ACTIVITY_REWARDS_ENABLED"), True),
    "min_messages_low": _coerce_int(os.getenv("ACTIVITY_REWARDS_MIN_MESSAGES_LOW"), 200, minimum=1),
    "min_messages_high": _coerce_int(os.getenv("ACTIVITY_REWARDS_MIN_MESSAGES_HIGH"), 300, minimum=1),
    "draw_interval_minutes": _coerce_int(os.getenv("ACTIVITY_REWARDS_DRAW_INTERVAL_MINUTES"), 360, minimum=5),
    "cooldown_days": _coerce_int(os.getenv("ACTIVITY_REWARDS_COOLDOWN_DAYS"), 7, minimum=0),
    "max_rewards_per_draw": 1,
    "daily_winner_cap": _coerce_int(os.getenv("ACTIVITY_REWARDS_DAILY_WINNER_CAP"), 3, minimum=1),
    "weekly_winner_cap": _coerce_int(os.getenv("ACTIVITY_REWARDS_WEEKLY_WINNER_CAP"), 12, minimum=1),
    "key_pool": str(os.getenv("ACTIVITY_REWARDS_KEY_POOL", "activity_pool")).strip().lower() or "activity_pool",
    "logging_channel_id": None,
}


def _coerce_activity_rewards_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(_ACTIVITY_REWARDS_DEFAULT)
    out.update({k: v for k, v in raw.items() if k in _ACTIVITY_REWARDS_DEFAULT})
    out["enabled"] = _coerce_bool(out.get("enabled"), _ACTIVITY_REWARDS_DEFAULT["enabled"])
    out["min_messages_low"] = _coerce_int(out.get("min_messages_low"), 200, minimum=1)
    out["min_messages_high"] = _coerce_int(out.get("min_messages_high"), 300, minimum=out["min_messages_low"])
    out["draw_interval_minutes"] = _coerce_int(out.get("draw_interval_minutes"), 360, minimum=5)
    out["cooldown_days"] = _coerce_int(out.get("cooldown_days"), 7, minimum=0)
    out["max_rewards_per_draw"] = min(5, _coerce_int(out.get("max_rewards_per_draw"), 1, minimum=1))
    out["daily_winner_cap"] = min(200, _coerce_int(out.get("daily_winner_cap"), 3, minimum=1))
    out["weekly_winner_cap"] = min(1000, _coerce_int(out.get("weekly_winner_cap"), 12, minimum=1))
    key_pool = str(out.get("key_pool") or "activity_pool").strip().lower()
    out["key_pool"] = key_pool if key_pool in {"activity_pool", "giveaway"} else "activity_pool"
    
    log_ch = out.get("logging_channel_id")
    out["logging_channel_id"] = str(log_ch) if log_ch else None
    
    for old_key in ["event_webhook_enabled", "event_webhook_url", "event_webhook_retry_count"]:
        out.pop(old_key, None)
        
    return stringify_ids(out)


@app.get("/api/guilds/{guild_id}/activity_rewards")
async def get_activity_rewards(guild_id: str):
    all_cfg = load_json(_ACTIVITY_REWARDS_CONFIG_PATH, {})
    stored = all_cfg.get(guild_id, {})
    merged = dict(_ACTIVITY_REWARDS_DEFAULT)
    if isinstance(stored, dict):
        merged.update(stored)
    if merged["min_messages_high"] < merged["min_messages_low"]:
        merged["min_messages_high"] = merged["min_messages_low"]
    return stringify_ids(merged)


@app.put("/api/guilds/{guild_id}/activity_rewards")
async def update_activity_rewards(guild_id: str, request: Request, body: Dict[str, Any] = Body(...)):
    all_cfg = load_json(_ACTIVITY_REWARDS_CONFIG_PATH, {})
    before_cfg = all_cfg.get(guild_id, {}) if isinstance(all_cfg.get(guild_id, {}), dict) else {}
    merged_body = _merge_shallow_dict(before_cfg, body if isinstance(body, dict) else {})
    coerced = _coerce_activity_rewards_payload(merged_body)
    all_cfg[guild_id] = coerced
    save_json(_ACTIVITY_REWARDS_CONFIG_PATH, all_cfg)
    await _append_config_audit_entry(
        request=request,
        guild_id=guild_id,
        module="activity_rewards",
        before=before_cfg,
        after=coerced,
    )
    return {"status": "success", "config": coerced}


@app.get("/api/guilds/{guild_id}/activity_rewards/status")
async def get_activity_rewards_status(guild_id: str):
    cfg = await get_activity_rewards(guild_id)
    stats_root = load_json(_ACTIVITY_REWARDS_STATS_PATH, {})
    state_root = load_json(_ACTIVITY_REWARDS_STATE_PATH, {})

    guild_stats = stats_root.get(guild_id, {}) if isinstance(stats_root, dict) else {}
    guild_state = state_root.get(guild_id, {}) if isinstance(state_root, dict) else {}
    if not isinstance(guild_stats, dict):
        guild_stats = {}
    if not isinstance(guild_state, dict):
        guild_state = {}

    user_rows = [v for v in guild_stats.values() if isinstance(v, dict)]
    eligible_users = 0
    for row in user_rows:
        messages = _coerce_int(row.get("message_count"), 0, minimum=0)
        if messages >= _coerce_int(cfg.get("min_messages_low"), 200, minimum=1):
            eligible_users += 1

    last_draw_at = guild_state.get("last_draw_at")
    next_draw_at = None
    parsed_last = None
    try:
        if last_draw_at:
            parsed_last = datetime.fromisoformat(str(last_draw_at).replace("Z", "+00:00"))
    except ValueError:
        parsed_last = None
    if parsed_last is not None:
        if parsed_last.tzinfo is None:
            parsed_last = parsed_last.replace(tzinfo=timezone.utc)
        next_draw_at = (parsed_last.astimezone(timezone.utc) + timedelta(
            minutes=_coerce_int(cfg.get("draw_interval_minutes"), 360, minimum=5)
        )).isoformat()

    history = guild_state.get("reward_history", [])
    if not isinstance(history, list):
        history = []
    claim_tracking = guild_state.get("key_claim_tracking", {})
    if not isinstance(claim_tracking, dict):
        claim_tracking = {}
    claim_status_counts: Dict[str, int] = {"pending": 0, "claimed": 0, "closed": 0, "voided": 0}
    for item in claim_tracking.values():
        if not isinstance(item, dict):
            continue
        status = str(item.get("claim_status") or "pending").strip().lower()
        if status not in claim_status_counts:
            claim_status_counts[status] = 0
        claim_status_counts[status] += 1

    return stringify_ids({
        "tracked_users": len(user_rows),
        "eligible_users": eligible_users,
        "last_draw_at": last_draw_at,
        "next_draw_at": next_draw_at,
        "recent": history[-15:],
        "claim_status_counts": claim_status_counts,
    })


@app.get("/api/guilds/{guild_id}/activity_rewards/leaderboard")
async def get_activity_rewards_leaderboard(guild_id: str, limit: int = 15):
    from modules.activity_rewards import get_activity_rewards_leaderboard as _get_lb

    safe_limit = max(1, min(50, int(limit or 15)))
    rows = _get_lb(int(guild_id), safe_limit)
    return stringify_ids({"items": rows, "count": len(rows)})

@app.post("/api/guilds/{guild_id}/activity_rewards/test_logging")
@app.post("/api/bot/guilds/{guild_id}/activity_rewards/test_logging")
async def test_activity_rewards_logging(guild_id: str, request: Request):
    from modules.ipc import process_dashboard_trigger
    await require_guild_dashboard_access(request, guild_id)
    result, status_code = await process_dashboard_trigger("activity_rewards_test_logging", guild_id, {})
    if status_code != 200:
        raise HTTPException(status_code=status_code, detail=result.get("message", result.get("error", "Unknown error")))
    return result

# --- Auto Moderation ---

_AUTOMOD_CONFIG_PATH = "automod_configs"

_AUTOMOD_DEFAULT: Dict[str, Any] = {
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


def _coerce_automod_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(_AUTOMOD_DEFAULT)
    out.update({k: v for k, v in raw.items() if k in _AUTOMOD_DEFAULT})
    out["enabled"] = _coerce_bool(out.get("enabled"), False)
    out["scan_links"] = _coerce_bool(out.get("scan_links"), True)
    out["scan_images"] = _coerce_bool(out.get("scan_images"), False)
    out["dm_user_on_action"] = _coerce_bool(out.get("dm_user_on_action"), True)
    out["timeout_minutes"] = _coerce_int(out.get("timeout_minutes"), 10, minimum=1)

    action = str(out.get("action") or "delete").strip().lower()
    if action not in {"delete", "timeout", "kick", "ban"}:
        action = "delete"
    out["action"] = action

    out["log_channel_id"] = _coerce_optional_str(out.get("log_channel_id"))
    out["dm_message"] = str(out.get("dm_message") or _AUTOMOD_DEFAULT["dm_message"])

    for list_key in ("exempt_role_ids", "exempt_channel_ids", "blocked_domains", "whitelist_domains", "blocked_image_hashes"):
        raw_list = out.get(list_key)
        if not isinstance(raw_list, list):
            out[list_key] = []
        else:
            out[list_key] = [str(v).strip() for v in raw_list if str(v).strip()]

    return stringify_ids(out)


@app.get("/api/guilds/{guild_id}/automod")
async def get_automod(guild_id: str):
    all_cfg = load_json(_AUTOMOD_CONFIG_PATH, {})
    stored = all_cfg.get(guild_id, {})
    merged = dict(_AUTOMOD_DEFAULT)
    merged.update(stored)
    return stringify_ids(merged)


@app.put("/api/guilds/{guild_id}/automod")
async def update_automod(guild_id: str, request: Request, body: Dict[str, Any] = Body(...)):
    all_cfg = load_json(_AUTOMOD_CONFIG_PATH, {})
    before_cfg = all_cfg.get(guild_id, {}) if isinstance(all_cfg.get(guild_id, {}), dict) else {}
    merged_body = _merge_shallow_dict(before_cfg, body if isinstance(body, dict) else {})
    coerced = _coerce_automod_payload(merged_body)
    all_cfg[guild_id] = coerced
    save_json(_AUTOMOD_CONFIG_PATH, all_cfg)
    await _append_config_audit_entry(
        request=request,
        guild_id=guild_id,
        module="automod",
        before=before_cfg,
        after=coerced,
    )
    return {"status": "success", "config": coerced}


# --- Anti-Spam ---

_ANTISPAM_CONFIG_PATH = "antispam_configs"

_ANTISPAM_DEFAULT: Dict[str, Any] = {
    "enabled": False,
    "threshold": 5,
    "window_seconds": 5,
    "action": "timeout",
    "timeout_minutes": 10,
    "delete_messages": True,
    "log_channel_id": None,
    "exempt_role_ids": [],
    "exempt_channel_ids": [],
    "dm_user_on_action": True,
    "dm_message": "You were actioned in this server for sending too many messages too quickly (spam).",
}


def _coerce_antispam_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(_ANTISPAM_DEFAULT)
    out.update({k: v for k, v in raw.items() if k in _ANTISPAM_DEFAULT})
    out["enabled"] = _coerce_bool(out.get("enabled"), False)
    out["delete_messages"] = _coerce_bool(out.get("delete_messages"), True)
    out["dm_user_on_action"] = _coerce_bool(out.get("dm_user_on_action"), True)
    out["threshold"] = min(20, _coerce_int(out.get("threshold"), 5, minimum=1))
    out["window_seconds"] = min(30, _coerce_int(out.get("window_seconds"), 5, minimum=1))
    out["timeout_minutes"] = _coerce_int(out.get("timeout_minutes"), 10, minimum=1)

    action = str(out.get("action") or "timeout").strip().lower()
    if action not in {"warn", "timeout", "kick", "ban"}:
        action = "timeout"
    out["action"] = action

    out["log_channel_id"] = _coerce_optional_str(out.get("log_channel_id"))
    out["dm_message"] = str(out.get("dm_message") or _ANTISPAM_DEFAULT["dm_message"])

    for list_key in ("exempt_role_ids", "exempt_channel_ids"):
        raw_list = out.get(list_key)
        if not isinstance(raw_list, list):
            out[list_key] = []
        else:
            out[list_key] = [str(v).strip() for v in raw_list if str(v).strip()]

    return stringify_ids(out)


@app.get("/api/guilds/{guild_id}/anti-spam")
async def get_antispam(guild_id: str):
    all_cfg = load_json(_ANTISPAM_CONFIG_PATH, {})
    stored = all_cfg.get(guild_id, {})
    merged = dict(_ANTISPAM_DEFAULT)
    merged.update(stored)
    return stringify_ids(merged)


@app.put("/api/guilds/{guild_id}/anti-spam")
async def update_antispam(guild_id: str, request: Request, body: Dict[str, Any] = Body(...)):
    all_cfg = load_json(_ANTISPAM_CONFIG_PATH, {})
    before_cfg = all_cfg.get(guild_id, {}) if isinstance(all_cfg.get(guild_id, {}), dict) else {}
    merged_body = _merge_shallow_dict(before_cfg, body if isinstance(body, dict) else {})
    coerced = _coerce_antispam_payload(merged_body)
    all_cfg[guild_id] = coerced
    save_json(_ANTISPAM_CONFIG_PATH, all_cfg)
    await _append_config_audit_entry(
        request=request,
        guild_id=guild_id,
        module="anti_spam",
        before=before_cfg,
        after=coerced,
    )
    return {"status": "success", "config": coerced}


# --- Ping Protection ---

_PINGPROTECT_CONFIG_PATH = "pingprotect_configs"

_PINGPROTECT_DEFAULT: Dict[str, Any] = {
    "enabled": False,
    "protected_user_ids": [],
    "protect_admins": True,
    "protected_role_ids": [],
    "exempt_role_ids": [],
    "exempt_channel_ids": [],
    "delete_message": False,
    "log_channel_id": None,
    "dm_user_on_warn": True,
    "warn_message": "⚠️ Please avoid unnecessarily pinging staff/admins. This is a warning.",
}


def _coerce_pingprotect_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(_PINGPROTECT_DEFAULT)
    out.update({k: v for k, v in raw.items() if k in _PINGPROTECT_DEFAULT})
    out["enabled"] = _coerce_bool(out.get("enabled"), False)
    out["protect_admins"] = _coerce_bool(out.get("protect_admins"), True)
    out["delete_message"] = _coerce_bool(out.get("delete_message"), False)
    out["dm_user_on_warn"] = _coerce_bool(out.get("dm_user_on_warn"), True)

    out["log_channel_id"] = _coerce_optional_str(out.get("log_channel_id"))
    out["warn_message"] = str(out.get("warn_message") or _PINGPROTECT_DEFAULT["warn_message"])

    for list_key in ("protected_user_ids", "protected_role_ids", "exempt_role_ids", "exempt_channel_ids"):
        raw_list = out.get(list_key)
        if not isinstance(raw_list, list):
            out[list_key] = []
        else:
            out[list_key] = [str(v).strip() for v in raw_list if str(v).strip()]

    return stringify_ids(out)


@app.get("/api/guilds/{guild_id}/ping-protection")
async def get_ping_protection(guild_id: str):
    all_cfg = load_json(_PINGPROTECT_CONFIG_PATH, {})
    stored = all_cfg.get(guild_id, {})
    merged = dict(_PINGPROTECT_DEFAULT)
    merged.update(stored)
    return stringify_ids(merged)


@app.put("/api/guilds/{guild_id}/ping-protection")
async def update_ping_protection(guild_id: str, request: Request, body: Dict[str, Any] = Body(...)):
    all_cfg = load_json(_PINGPROTECT_CONFIG_PATH, {})
    before_cfg = all_cfg.get(guild_id, {}) if isinstance(all_cfg.get(guild_id, {}), dict) else {}
    merged_body = _merge_shallow_dict(before_cfg, body if isinstance(body, dict) else {})
    coerced = _coerce_pingprotect_payload(merged_body)
    all_cfg[guild_id] = coerced
    save_json(_PINGPROTECT_CONFIG_PATH, all_cfg)
    await _append_config_audit_entry(
        request=request,
        guild_id=guild_id,
        module="ping_protection",
        before=before_cfg,
        after=coerced,
    )
    return {"status": "success", "config": coerced}


# --- Triggers (Proxy to main.py) ---
@app.post("/api/trigger/{action}")
@app.post("/api/bot/trigger/{action}")
async def trigger_action(action: str, req: TriggerRequest, request: Request):
    await require_guild_dashboard_access(request, req.guild_id)
    
    from modules.ipc import process_dashboard_trigger
    result, status_code = await process_dashboard_trigger(action, req.guild_id, req.payload)
    if status_code != 200:
        raise HTTPException(status_code=status_code, detail=result.get("message") or str(result))
    return result


@app.middleware("http")
async def seisen_guild_path_auth(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    m = _DASHBOARD_GUILD_PATH_RE.match(path)
    if not m:
        return await call_next(request)
    # Logged-in session only. "Manage server" is enforced when listing servers (/api/bot/guilds)
    # and for trigger actions — not on every guild config GET/PUT.
    try:
        await require_authenticated_discord_user(request)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)

