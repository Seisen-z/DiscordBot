"""
modules/ipc.py
Dashboard IPC Server & Settings API
"""

from __future__ import annotations

import base64
import os

import discord
from aiohttp import web
import uvicorn
from discord.ext import commands

# Dependencies from other modules would normally be imported here.
# For circular dependency avoidance, we will receive `bot` in register.
_bot: commands.Bot | None = None


def _resolve_dashboard_api_port() -> int:
    """Resolve API bind port with host panel compatibility.

    In managed hosts, `SERVER_PORT` (or `PORT`) is usually the only reachable
    public port. Prefer it over stale `API_PORT` values copied from old nodes.
    """
    panel_port = str(os.getenv("SERVER_PORT", "")).strip() or str(os.getenv("PORT", "")).strip()
    api_port = str(os.getenv("API_PORT", "")).strip()

    def _parse_port(raw: str) -> int | None:
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        if 1 <= value <= 65535:
            return value
        return None

    panel_value = _parse_port(panel_port)
    api_value = _parse_port(api_port)

    if panel_value is not None:
        if api_value is not None and api_value != panel_value:
            print(
                f"[API] API_PORT={api_value} differs from SERVER_PORT/PORT={panel_value}. "
                f"Using {panel_value} so the API remains externally reachable."
            )
        return panel_value

    if api_value is not None:
        return api_value

    return 9820


def _resolve_ipc_port() -> int:
    """Resolve legacy IPC listener port.

    If IPC_PORT is not configured, align with API port so the legacy IPC
    listener is skipped by default on single-port hosts.
    """
    fallback_port = _resolve_dashboard_api_port()
    raw = str(os.getenv("IPC_PORT", "")).strip()
    if not raw:
        return fallback_port

    try:
        value = int(raw)
    except ValueError:
        print(f"[IPC] Invalid IPC_PORT={raw!r}. Falling back to {fallback_port}.")
        return fallback_port

    if not (1 <= value <= 65535):
        print(f"[IPC] IPC_PORT={value} is out of range. Falling back to {fallback_port}.")
        return fallback_port

    return value


async def process_dashboard_trigger(action: str, guild_id_str: str | None, payload: dict | None = None) -> tuple[dict, int]:
    """Run dashboard trigger action against the connected bot state."""
    payload = payload if isinstance(payload, dict) else {}

    if not guild_id_str:
        return {"status": "error", "message": "Missing guild_id"}, 400

    try:
        guild_id = int(guild_id_str)
    except (TypeError, ValueError):
        return {"status": "error", "message": "Invalid guild_id"}, 400

    if _bot is None:
        return {"status": "error", "message": "Bot is not ready yet"}, 503

    guild = _bot.get_guild(guild_id)
    if not guild:
        return {"status": "error", "message": f"Guild {guild_id} not found by bot"}, 404

    from modules.dashboard_handlers import on_dashboard_trigger

    result = await on_dashboard_trigger(action, guild, payload)
    if not isinstance(result, dict):
        result = {"status": "success", "action": action}

    status_code = 200
    if str(result.get("status", "")).lower() != "success":
        status_code = int(result.get("http_status") or 400)

    return result, status_code

async def handle_dashboard_trigger(request: web.Request):
    action = request.match_info.get('action')
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")
    guild_id_str = data.get("guild_id")
    payload = data.get("payload")
    result, status_code = await process_dashboard_trigger(action, guild_id_str, payload)

    return web.json_response(result, status=status_code)


async def start_ipc_server():
    ipc_port = _resolve_ipc_port()
    api_port = _resolve_dashboard_api_port()

    if ipc_port == api_port:
        print(
            f"[IPC] Skipping legacy IPC server because IPC_PORT ({ipc_port}) matches API port ({api_port})."
        )
        return

    try:
        ipc_app = web.Application()
        ipc_app.router.add_post('/trigger/{action}', handle_dashboard_trigger)
        runner = web.AppRunner(ipc_app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', ipc_port)
        await site.start()
        print(f"[IPC] Bot Internal Server started on http://0.0.0.0:{ipc_port}")
    except OSError as e:
        print(f"[IPC] Failed to bind {ipc_port}: {e}. Bot will continue running.")


async def start_fastapi_server():
    try:
        from api import app as fastapi_app
        API_PORT = _resolve_dashboard_api_port()
        config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=API_PORT, log_level="info")
        server = uvicorn.Server(config)
        print(f"[API] Dashboard FastAPI server starting on port {API_PORT}")
        await server.serve()
    except SystemExit:
        print(f"[API] Port is already in use. Skipping embedded FastAPI startup.")
    except OSError as e:
        print(f"[API] Failed to start embedded FastAPI: {e}")
    except ImportError as import_err:
        print(f"[API] Could not import fastapi app from api.py: {import_err}")


def register(bot: commands.Bot):
    global _bot
    _bot = bot
    # Tasks are scheduled in on_ready() / setup_hook() to avoid
    # accessing bot.loop in a non-async context (discord.py v2.x restriction).